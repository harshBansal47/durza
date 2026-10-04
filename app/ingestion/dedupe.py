"""Deduplication: one canonical coupon, however many providers advertise it.

Four networks will hand us the same `SAVE20` at Nike. The shopper must see it
once. The providers' individual records must all survive. Those two requirements
together are the whole of this module.

## Why the identity key is (merchant, normalized code)

For a coupon *with* a code, the code is the strongest identity signal available,
because it is the one string the merchant itself recognises. Within a single
merchant, `SAVE20` is one code — every provider is reporting the same fact about
it, just with different metadata and a different affiliate URL. Discount values,
titles and dates are all provider *interpretations*; the code is not.

The merchant is half the key on purpose. Two different retailers running a
promo called `SUMMER` are two coupons, and merging them would send half the
audience to a store the code does not work at.

## Why code-less offers need a fingerprint instead

A deal with no code has no identity of its own — "20% off everything" is not a
distinguishing fact, and there can be several at one merchant. So identity falls
back to a fingerprint over merchant + normalized title + discount + offer type +
validity window. That is deliberately a *narrow* fingerprint: it is meant to
catch the same deal arriving twice, not to cluster similar deals. Two genuinely
different offers can produce the same fingerprint and be merged incorrectly, which
is why the fingerprint is only used when there is no code and the deal has to be
stored at all.

## What this module deliberately refuses to do

Merge on similar titles. "20% Off Everything" and "20% Off Shoes" at the same
store are two coupons, and collapsing them loses a real one. Title similarity is
available via `merchants.name_similarity` for an admin to *inspect* duplicate
candidates, and is not used to merge. Over-merging is the expensive error: it
sends a shopper to a code that does not work and cannot be undone quietly.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime

from app.ingestion import merchants
from app.ingestion.normalize import NormalizedOffer
from app.models.coupon import DiscountType

# Domain separators around each component of a key. Without them,
# ("ab", "c") and ("a", "bc") would hash identically — a real collision, not a
# theoretical one, since store slugs and codes are attacker-influenced strings.
_SEP = "\x1f"


def _digest(parts: list[str]) -> str:
    return hashlib.sha256(_SEP.join(parts).encode()).hexdigest()


def normalize_title(title: str | None) -> str:
    """Canonical form of an offer headline for fingerprinting.

    Lowercased, punctuation stripped, whitespace collapsed, and marketing noise
    removed. Only applied to the *code-less* fingerprint path — with a code, the
    title is a provider's wording and must never affect identity.
    """
    if not title:
        return ""
    text = title.strip().lower()
    text = merchants.NON_ALNUM.sub(" ", text)
    return " ".join(text.split())


def _discount_component(offer: NormalizedOffer) -> str:
    """Discount as a comparable string.

    Two providers describing the same 20% offer must produce the same string, so
    the *type* is part of the key: "percentage 20" and "fixed 20" are different
    offers even though the number matches. An absent value is a distinct token
    rather than an empty one, so a deal with no stated discount does not collide
    with one that states 0.
    """
    if offer.discount_value is None:
        return f"{offer.discount_type.value}:none"
    return f"{offer.discount_type.value}:{offer.discount_value:g}"


def _validity_component(offer: NormalizedOffer) -> str:
    """The validity window, bucketed to the day.

    Day granularity rather than the instant: providers report the same expiry as
    `2026-12-30T00:00:00Z`, `2026-12-30T23:59:59Z` and `2026-12-30` depending on
    their API. Comparing to the second would make every provider's copy of one
    coupon look like a different coupon, which is precisely the duplicate the
    dedupe layer exists to remove.

    `none` is a real value: an offer with no end date and one that ends in 2020
    are not the same offer, and must not collide.
    """
    if offer.expires_at is None:
        return "none"
    return offer.expires_at.date().isoformat()


@dataclass(frozen=True)
class DedupKey:
    """A resolved identity, plus *how* it was reached.

    `strategy` is carried rather than discarded because it changes what a match
    means: a `code` match is strong evidence, a `fingerprint` match is a
    judgement call. Anything that needs to know how confident a merge was — the
    audit trail, an admin review queue, a later un-merge — reads it from here.
    """

    value: str
    strategy: str  # "code" | "fingerprint"


def dedup_key(offer: NormalizedOffer) -> DedupKey:
    """The canonical identity for an offer.

    The code path is preferred and is not a fallback: if two providers disagree
    about a code-less deal's wording but agree it is `SAVE20` at Nike, that is
    one coupon and the disagreement is only in the description.
    """
    merchant = offer.store_slug or ""
    code = merchants.normalize_code(offer.code)

    if code:
        return DedupKey(
            value=_digest(["code", merchant, code]),
            # Named, not hashed: the code itself is what a human needs to see in
            # an audit or a duplicate-review queue.
            strategy=f"code:{merchant}:{code}",
        )

    return DedupKey(
        value=_digest(
            [
                "fingerprint",
                merchant,
                normalize_title(offer.title),
                _discount_component(offer),
                offer.discount_type.value,
                _validity_component(offer),
            ]
        ),
        # The fingerprint's inputs, so a reviewer can see *why* two records were
        # considered the same offer rather than being handed an opaque hash.
        strategy=(
            f"fingerprint:{merchant}:{normalize_title(offer.title)[:60]}"
            f":{_discount_component(offer)}:{_validity_component(offer)}"
        ),
    )


def content_hash(offer: NormalizedOffer) -> str:
    """The stored `coupons.content_hash` value.

    Kept as the single hash column and as a function name because it is already
    indexed and already referenced by the admin panel and by
    `tests/test_ingestion.py`. Its *meaning* is now the full dedup key rather
    than a `(store, code-or-title)` pair that ignored the discount, which was
    the old behaviour's one genuine flaw: it merged two different percentage
    discounts at one store into one coupon and silently kept whichever the
    provider mentioned last.
    """
    return dedup_key(offer).value


def same_coupon(left: NormalizedOffer, right: NormalizedOffer) -> bool:
    """Whether two offers are the same canonical coupon.

    Convenience over `dedup_key` so callers do not each re-implement the
    comparison, and so the "code wins over fingerprint" rule is stated once.
    """
    return dedup_key(left).value == dedup_key(right).value


def describe_conflict(
    winner: NormalizedOffer, loser: NormalizedOffer
) -> dict[str, str]:
    """Field-level differences between two providers' view of one coupon.

    Used to build the audit trail when conflict resolution picks a winner. Without
    it, a disputed expiry date looks exactly like agreement, and the next person
    to look has no way of knowing a decision was made.
    """
    return {
        "expires_at": _diff(winner.expires_at, loser.expires_at),
        "discount": _diff(
            (winner.discount_type.value, winner.discount_value),
            (loser.discount_type.value, loser.discount_value),
        ),
        "title": _diff(winner.title, loser.title),
    }


def _diff(winner_value: object, loser_value: object) -> str:
    if winner_value == loser_value:
        return "agreed"
    return f"winner={winner_value!r} loser={loser_value!r}"


def expiry_conflict_resolution(
    claims: list[tuple[int, datetime | None]],
) -> datetime | None:
    """Pick one expiry date from several providers' claims.

    `claims` is (source_priority, expires_at) per source — lower priority number
    means more trusted. The rule is the conservative one: among the sources we
    trust equally, take the **earliest** expiry.

    Taking the latest date would keep a coupon visible for as long as possible,
    which sounds better for click volume and is much worse for the shopper, who
    arrives on a day the code stopped working. When two providers genuinely
    disagree, the earlier date is the one that cannot produce a broken promise.

    `None` means "no known expiry" and is only chosen when every source says so,
    or when there is nothing to choose from — one unknown claim must not erase a
    known date, because providers omit expiry far more often than they get it
    wrong.
    """
    if not claims:
        return None

    best_priority = min(priority for priority, _ in claims)
    eligible = [expires for priority, expires in claims if priority == best_priority]
    known = [expires for expires in eligible if expires is not None]
    if not known:
        return None
    return min(known)


def discount_type_rank(offer: NormalizedOffer) -> int:
    """Sort key for discounts of the same magnitude.

    A percentage is generally a better offer than the same number of currency
    off, because it scales with basket size. Only used as a tiebreak when two
    sources report the same numeric discount with different types.
    """
    if offer.discount_type is DiscountType.percentage:
        return 0
    if offer.discount_type is DiscountType.fixed:
        return 1
    return 2
