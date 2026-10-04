"""Merchant identity: deciding that "NIKE", "Nike.com" and "www.nike.com" are one merchant.

This is the step that decides whether a coupon site has one Nike page or nine.
Get it wrong in the direction of *merging*, and two genuinely different shops
become one page where every coupon is wrong for half the audience. Get it wrong
in the direction of *splitting*, and you get a duplicate-merchant problem that
is annoying but not incorrect.

So the signals are tried strongest-first and each one has to earn its place:

1. **Normalized domain.** `nike.com` from any provider is `nike.com`. A
   registrable-ish hostname is a near-proof of identity, which is why
   `stores.normalized_domain` carries a UNIQUE constraint.
2. **A known alias.** Explicit, human-curated, auditable. Resolves the cases
   where two providers use unrelated names for one merchant.
3. **Exact normalized name.** `NIKE` and `Nike` collapse; `Nike` and
   `Nike Store` do not, because they might be different things.
4. **Fuzzy name match.** Off by default, and never used to *create* a merge.
   It is a candidate generator for a human to confirm, not a decision. Two
   retailers called "Prime" merging is the failure mode that costs trust.

Cross-provider merchant IDs are handled where they are trustworthy: a program id
from the same network is an identity, a program id from a *different* network is
not, because Rakuten's advertiser 1234 and CJ's advertiser 1234 are unrelated.
See `merchant_ids_match`.
"""

import re
from difflib import SequenceMatcher
from urllib.parse import urlparse

# Public-suffix-ish handling without a dependency on the PSL. The list is
# deliberately short: it covers the multi-part suffixes that actually appear in
# retailer domains. Getting it wrong in the "too short" direction
# (`co.uk` treated as a single label) costs a few extra candidate rows, which is
# far less harmful than collapsing `shop.example.com.au` into one merchant.
_MULTI_PART_SUFFIXES = frozenset(
    {
        "co.uk", "org.uk", "me.uk", "ac.uk", "gov.uk", "co.jp", "or.jp", "ne.jp",
        "com.au", "net.au", "org.au", "co.nz", "com.br", "com.mx", "co.in",
        "co.za", "com.sg", "com.tr", "co.kr", "com.cn", "com.hk", "com.tw",
    }
)

# Subdomains that are a storefront's own front door rather than a separate site.
# Stripping these is what makes `m.nike.com` and `www.nike.com` one merchant.
_GENERIC_SUBDOMAINS = frozenset(
    {"www", "m", "en", "us", "uk", "in", "shop", "store", "stores", "outlet", "sale", "deals"}
)

# Marketing noise that providers append to merchant names. "Nike Official Store"
# and "Nike" are the same merchant; stripping the qualifier is what makes the
# exact-normalized-name step work on them.
_NAME_SUFFIXES = (
    "official store",
    "official site",
    "official website",
    "online store",
    "online shop",
    "the store",
    "store",
    "shop",
    "outlet",
)

#: Characters removed by `normalize_code`. Public because dedupe needs the same
#: notion of "what counts as a separator" for titles, and the two must agree —
#: a code and a title that disagree about punctuation produce two halves of one
#: normalization strategy that silently never matches.
#:
#: Case-*insensitive*, which is not cosmetic. The class was `[^a-z0-9]+`, so a
#: conventional uppercase code like `SAVE20` matched it in full and was
#: substituted away to an empty string: `normalize_code("SAVE20")` returned
#: `None`, and every coded offer silently fell through to the code-less
#: fingerprint path instead. Since nearly every provider sends codes uppercase,
#: that quietly disabled the *primary* identity key of the whole design
#: (merchant + normalized_code) and left dedupe depending on title wording —
#: which then split `SAVE20` and `save20` at the same merchant into two coupons
#: whenever their headlines differed. It surfaced as a failing
#: `test_content_hash_ignores_discount_changes`: both offers were fingerprinted
#: with their discounts, so two different codes' worth of "same coupon" claim
#: failed, and the real cause was one character in a regex.
NON_ALNUM = re.compile(r"[^a-z0-9]+", re.IGNORECASE)


def normalize_domain(value: str | None) -> str | None:
    """Reduce any URL or hostname to its registrable-ish domain, or None.

    Handles the four shapes providers actually send: a full URL, a bare host
    with a scheme-less prefix, a `www.` host, and a host with a port or path
    left on it. Returns None rather than guessing when there is no dot at all
    (`localhost`, a bare brand word), because inventing a domain is how two
    unrelated merchants get merged.
    """
    if not value:
        return None

    candidate = value.strip().lower()
    if not candidate:
        return None

    # A full URL: take the host and discard scheme, path, query and fragment.
    if "//" in candidate:
        parsed = urlparse(candidate)
        candidate = parsed.hostname or ""
    else:
        # Strip any accidental path/query that came along.
        candidate = candidate.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        # Strip a port, but only a numeric one — "example.com:8080" is a host
        # and port, "example.com:something" is not a thing.
        if ":" in candidate:
            host, _, port = candidate.rpartition(":")
            if port.isdigit():
                candidate = host

    candidate = candidate.strip(".")
    if not candidate or "." not in candidate:
        return None

    labels = [label for label in candidate.split(".") if label]
    if len(labels) < 2:
        return None

    # Drop generic storefront subdomains, but never strip the registrable label.
    while len(labels) > 2 and labels[0] in _GENERIC_SUBDOMAINS:
        labels.pop(0)

    tail2 = ".".join(labels[-2:])
    keep = 2 if tail2 in _MULTI_PART_SUFFIXES else 1
    domain = ".".join(labels[-keep:])

    # A domain made only of digits is an IP fragment or a mis-parse.
    if not domain or domain.replace(".", "").isdigit():
        return None
    return domain


def normalize_name(value: str | None) -> str:
    """Casefold a merchant name to a comparable form.

    Strips punctuation, collapses whitespace, and removes trailing marketing
    qualifiers. This is deliberately *not* aggressive: "Nike Store" and
    "Nike Stores" collapse to the same thing, but "Nike" and "Niketown" stay
    apart, because the failure this guards against is over-merging.
    """
    if not value:
        return ""

    text = value.strip().lower()
    text = re.sub(r"[\s\-_/,|]+", " ", text)
    text = re.sub(r"[^\w\s]", "", text)
    text = " ".join(text.split())

    # "Nike Official Store" -> "nike". Repeated because the qualifiers stack
    # ("Nike Store Outlet" needs two passes).
    changed = True
    while changed:
        changed = False
        for suffix in _NAME_SUFFIXES:
            if text.endswith(f" {suffix}") and len(text) > len(suffix) + 1:
                text = text[: -len(suffix)].strip()
                changed = True
    return text


def normalize_code(value: str | None) -> str | None:
    """Canonical form of a coupon code for cross-provider identity.

    `"save20"`, `"SAVE 20"` and `"Save-20"` are the same code — users type them
    with whatever separator they saw, and providers disagree about it
    inconsistently. `"SAVE20"` and `"SAVE-20"` are genuinely different codes
    (merchants do issue both), so the separator is removed rather than mapped to
    a fixed character: removing it would collapse codes that mean different
    things.

    Returns None for a code-less offer, which must be deduplicated by
    fingerprint instead.
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    compact = NON_ALNUM.sub("", stripped).upper()
    return compact or None


def name_similarity(a: str, b: str) -> float:
    """0..1 similarity of two normalized names. Only for *candidate* generation.

    Not for deciding a merge. Kept here rather than in the pipeline so that the
    threshold lives beside the other matching rules and can be reviewed as one
    piece.
    """
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def merchant_ids_match(left: dict[str, str], right: dict[str, str]) -> bool:
    """Whether two providers' program ids agree.

    Only ids from the *same* network are comparable. `rakuten:advertiser_id=1234`
    and `cj:advertiser_id=1234` are two different merchants that happen to share
    a number; treating them as equal would merge two unrelated retailers and
    attach one shop's coupons to another. Cross-network identity is established
    by domain, which is why that step runs first.
    """
    shared = set(left) & set(right)
    if not shared:
        return False
    # Every shared key must agree. One matching key alongside a disagreeing one
    # is a signal the data is wrong, not a reason to merge.
    return all(left[key] == right[key] for key in shared)


def merchant_from_domain(domain: str) -> str:
    """A display-friendly merchant name guessed from a domain.

    Only used when a provider supplies a URL but no name at all. `nike.com` ->
    "Nike". A better answer than the raw hostname, and clearly worse than a real
    provider-supplied name — which is why it is the *last* option, never the
    first.
    """
    label = domain.split(".")[0]
    # "my-store" -> "My Store", "shop2" -> "Shop2"
    return re.sub(r"[-_]+", " ", label).title()
