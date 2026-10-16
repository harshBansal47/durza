"""Resolving what a coupon should say when providers disagree.

Rakuten says `SAVE20` expires on the 30th. CJ says the 31st. Neither is
necessarily wrong — one may be reporting the merchant's campaign end date and the
other the code's own expiry — and there is no way to find out from here.

The rule this module follows is therefore not "who is right" but "which choice
fails more safely":

* Expiry resolves to the **earliest** claim among the most-trusted sources.
  Showing a coupon one day too long sends a shopper to a dead code; hiding it one
  day early costs nothing they would have had.
* A source that states *no* expiry never erases a known one. Providers omit
  expiry constantly and are rarely wrong about the ones they do state.
* Discount, title and description resolve to the **highest-priority source that
  actually supplied a value**. Not the highest-priority source — a priority-1
  source that omitted the discount must not blank out a priority-5 source's
  perfectly good one.

Every decision is recorded with a reason, so the audit trail can answer "why does
this say the 30th" without anyone re-reading four providers' feeds.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezones import ensure_utc
from app.ingestion.normalize import NormalizedOffer
from app.models.coupon import DiscountType
from app.models.coupon_source import CouponSource, CouponSourceStatus
from app.models.source import Source

# Below this, a source is treated as untrustworthy for conflict resolution even
# if it is the only one with a value. Below zero trust, taking a source's number
# is worse than having none.
MIN_USABLE_CONFIDENCE = 0.1


@dataclass
class SourceClaim:
    """One provider's contribution to a coupon's canonical representation."""

    source: Source
    offer: NormalizedOffer

    @property
    def priority(self) -> int:
        return self.source.priority

    @property
    def confidence(self) -> float:
        return self.source.confidence

    @property
    def usable(self) -> bool:
        return self.confidence >= MIN_USABLE_CONFIDENCE


@dataclass
class ResolutionDecision:
    """One resolved field, and why it resolved that way.

    `alternatives` carries the losing claims. This is the part that makes the
    mechanism auditable: without it, a disputed expiry date is indistinguishable
    from an agreed one, and the next person to investigate has to re-derive it.
    """

    field_name: str
    chosen: Any
    chosen_from: str | None
    reason: str
    alternatives: dict[str, Any] = field(default_factory=dict)
    conflict: bool = False


@dataclass
class ResolvedCoupon:
    """The canonical, public-facing view of a coupon."""

    title: str
    description: str | None
    discount_type: DiscountType
    discount_value: float | None
    starts_at: datetime | None
    expires_at: datetime | None
    destination_url: str
    decisions: list[ResolutionDecision] = field(default_factory=list)

    @property
    def had_conflict(self) -> bool:
        return any(decision.conflict for decision in self.decisions)

    def as_audit(self) -> dict[str, Any]:
        """The decisions, shaped for `raw_data` and the admin conflict view."""
        return {
            decision.field_name: {
                "chosen": decision.chosen,
                "from": decision.chosen_from,
                "reason": decision.reason,
                "conflict": decision.conflict,
                "alternatives": decision.alternatives,
            }
            for decision in self.decisions
        }


def _ordered(claims: list[SourceClaim]) -> list[SourceClaim]:
    """Most-trusted first. The key itself lives on `Source.precedence`.

    Ordering by confidence second is not decoration: two sources commonly share a
    priority (every source defaults to 100), and without a deterministic
    tiebreak the same inputs would resolve differently on two runs, which makes
    every conflict impossible to reproduce.
    """
    return sorted(claims, key=lambda claim: claim.source.precedence)


def _first_supplied(
    claims: list[SourceClaim], getter, field_name: str
) -> ResolutionDecision | None:
    """The first claim (most trusted) that actually supplied a value."""
    seen: dict[str, Any] = {}
    winner: SourceClaim | None = None
    for claim in claims:
        value = getter(claim.offer)
        if value is None:
            continue
        seen[claim.source.slug] = value
        if claim.usable and winner is None:
            winner = claim
    if winner is None:
        if not seen:
            return None
        # Every claim came from a source we trust less than the threshold. Take
        # the most-trusted one anyway — a poor value is better than nothing,
        # because nothing renders as "no discount" on the card.
        winner = next(c for c in claims if c.source.slug in seen)
        reason = "no source met the confidence threshold; used the most trusted available"
    else:
        reason = "highest-priority source that supplied a value"
    chosen = getter(winner.offer)
    return ResolutionDecision(
        field_name=field_name,
        chosen=chosen,
        chosen_from=winner.source.slug,
        reason=reason,
        alternatives={slug: value for slug, value in seen.items() if slug != winner.source.slug},
        conflict=len({_hashable(value) for value in seen.values()}) > 1,
    )


def _hashable(value: Any) -> Any:
    """Make a value comparable for "did the sources actually disagree?".

    Floats are rounded before comparison: two providers reporting a 20% discount
    as `20.0` and `20.000001` are agreeing to within noise, and flagging that as
    a conflict would make the audit trail useless by being permanently red.
    """
    if isinstance(value, float):
        return round(value, 4)
    return value


def resolve(claims: list[SourceClaim]) -> ResolvedCoupon:
    """Produce the canonical representation from every source's claim.

    Does not touch the database and does not decide *who* the coupon belongs to —
    dedup has already established that these claims are about one coupon. This
    function only decides what that coupon should say.
    """
    if not claims:
        raise ValueError("resolve() needs at least one claim")

    ordered = _ordered(claims)
    decisions: list[ResolutionDecision] = []

    def take(getter, field_name: str, default: Any) -> Any:
        decision = _first_supplied(ordered, getter, field_name)
        if decision is None:
            decisions.append(
                ResolutionDecision(
                    field_name=field_name,
                    chosen=default,
                    chosen_from=None,
                    reason="no source supplied this field",
                )
            )
            return default
        decisions.append(decision)
        return decision.chosen

    title = take(lambda o: o.title or None, "title", ordered[0].offer.title)
    description = take(lambda o: o.description or None, "description", None)
    discount_type = take(
        lambda o: o.discount_type if o.discount_value is not None or o.discount_type else None,
        "discount_type",
        ordered[0].offer.discount_type,
    )
    discount_value = take(lambda o: o.discount_value, "discount_value", None)

    # Expiry gets its own rule: earliest-wins among the most trusted, and a
    # missing claim never overwrites a known one. See the module docstring.
    expiry = _resolve_expiry(ordered)
    if expiry is not None:
        decisions.append(expiry)

    starts = _resolve_starts(ordered)
    if starts is not None:
        decisions.append(starts)

    destination = take(
        lambda o: o.destination_url or None, "destination_url", ordered[0].offer.destination_url
    )

    return ResolvedCoupon(
        title=title,
        description=description,
        discount_type=discount_type,
        discount_value=discount_value,
        starts_at=starts.chosen if starts else None,
        expires_at=expiry.chosen if expiry else None,
        destination_url=destination,
        decisions=decisions,
    )


def _resolve_expiry(ordered: list[SourceClaim]) -> ResolutionDecision | None:
    """Earliest expiry among the most-trusted tier that stated one."""
    stated = [claim for claim in ordered if claim.offer.expires_at is not None]
    if not stated:
        return None

    best_priority = stated[0].priority
    tier = [claim for claim in stated if claim.priority == best_priority]
    dates = {claim.source.slug: claim.offer.expires_at for claim in tier}
    # Conflicting claims at the same priority: take the earliest so the shopper
    # is never sent to a code that has already stopped working.
    chosen_slug = min(
        tier, key=lambda claim: claim.offer.expires_at or datetime.max.replace(tzinfo=UTC)
    ).source.slug

    unknown_at_tier = [
        claim.source.slug
        for claim in ordered
        if claim.offer.expires_at is None and claim.priority <= best_priority
    ]

    return ResolutionDecision(
        field_name="expires_at",
        chosen=dates[chosen_slug],
        chosen_from=chosen_slug,
        reason=(
            "earliest expiry among the highest-priority sources that stated one; "
            "a source with no expiry never erases a known one"
        ),
        alternatives={
            slug: value.isoformat() if value is not None else "not stated"
            for slug, value in dates.items()
            if slug != chosen_slug
        }
        | {slug: "not stated" for slug in unknown_at_tier},
        conflict=len(set(dates.values())) > 1,
    )


def _resolve_starts(ordered: list[SourceClaim]) -> ResolutionDecision | None:
    """Earliest start date, so a coupon never appears before it is usable."""
    stated = [claim for claim in ordered if claim.offer.starts_at is not None]
    if not stated:
        return None
    chosen = min(
        stated, key=lambda claim: claim.offer.starts_at or datetime.max.replace(tzinfo=UTC)
    )
    return ResolutionDecision(
        field_name="starts_at",
        chosen=chosen.offer.starts_at,
        chosen_from=chosen.source.slug,
        reason="earliest start among sources that stated one",
        alternatives={
            claim.source.slug: claim.offer.starts_at.isoformat()
            if claim.offer.starts_at is not None
            else "not stated"
            for claim in stated
            if claim.source.slug != chosen.source.slug
        },
        conflict=len({claim.offer.starts_at for claim in stated}) > 1,
    )


async def update_coupon_sources(
    db: AsyncSession, coupon_id, claims: list[SourceClaim]
) -> list[CouponSource]:
    """Write the per-source rows, recording what each provider said.

    `source_status` is set per claim and nothing is downgraded here. A source that
    simply did not appear in this run is *absent*, which is different from having
    reported the coupon as gone — the distinction that stops one flaky API call
    from retiring a working coupon. `mark_unseen_sources` handles absence, and it
    requires a completed run to act at all.
    """
    stored: list[CouponSource] = []
    now = datetime.now(UTC)

    for claim in claims:
        offer = claim.offer
        result = await db.execute(
            select(CouponSource).where(
                CouponSource.coupon_id == coupon_id, CouponSource.source_id == claim.source.id
            )
        )
        row = result.scalar_one_or_none()

        expired = offer.expires_at is not None and offer.expires_at <= now
        status = CouponSourceStatus.expired if expired else CouponSourceStatus.active

        if row is None:
            row = CouponSource(
                coupon_id=coupon_id,
                source_id=claim.source.id,
                status=status,
                affiliate_url=offer.destination_url,
                external_id=offer.external_id,
                source_expires_at=offer.expires_at,
                source_title=offer.title,
                raw_data=_slim_raw(offer),
                last_seen_at=now,
                source_updated_at=offer.expires_at,
            )
            db.add(row)
        else:
            row.status = status
            row.affiliate_url = offer.destination_url
            row.external_id = offer.external_id
            row.source_expires_at = offer.expires_at
            row.source_title = offer.title
            row.raw_data = _slim_raw(offer)
            row.last_seen_at = now
            row.source_updated_at = offer.expires_at
        stored.append(row)

    return stored


def _slim_raw(offer: NormalizedOffer) -> dict[str, Any]:
    """What to keep of a provider payload.

    `extra` is truncated to the keys that actually matter. Provider payloads run
    to tens of kilobytes of tracking parameters and nested ad-slot metadata; one
    row per coupon per provider at that size turns `coupon_sources` into the
    largest table in the database and slows every backup. The keys kept here are
    the ones a future re-resolution would need.
    """
    return {
        "restrictions": offer.restrictions,
        "source_categories": offer.source_categories,
        "country": offer.country,
        "currency": offer.currency,
        "external_id": offer.external_id,
    }


async def mark_unseen_sources(
    db: AsyncSession, coupon_id, source_ids: list, *, as_status: CouponSourceStatus
) -> int:
    """Downgrade source rows for providers absent from a *completed* run.

    Only ever called after a run that fetched to the end without error. That
    restriction is the whole point: an API that returned a 500 on page 3 of 40
    has not told us the other 37 pages' coupons are gone, and treating silence
    as removal is how a network outage deletes a catalogue.
    """
    if not source_ids:
        return 0
    result = await db.execute(
        select(CouponSource).where(
            CouponSource.coupon_id == coupon_id,
            CouponSource.source_id.notin_(source_ids),
            CouponSource.status == CouponSourceStatus.active,
        )
    )
    rows = list(result.scalars().all())
    for row in rows:
        row.status = as_status
    return len(rows)


async def mark_stale_for_source(
    db: AsyncSession,
    source_id,
    *,
    run_started_at: datetime,
    as_status: CouponSourceStatus,
) -> int:
    """Downgrade one source's rows it did not mention in a *completed* run.

    The source-scoped mirror of `mark_unseen_sources`: "this provider finished
    listing its catalogue and these rows were not in it".

    `run_started_at` is the only safe boundary to compare `last_seen_at` against.
    Using "now" instead would mark as stale every row that was simply written
    after the fetch finished, and using a fixed window would mark rows stale
    whenever two runs of one source overlapped.

    Returns the count downgraded. Callers must only invoke this for a run that
    reached the end of the provider's list — see `mark_unseen_sources`.
    """
    result = await db.execute(
        select(CouponSource).where(
            CouponSource.source_id == source_id,
            CouponSource.status == CouponSourceStatus.active,
            # `last_seen_at IS NULL` counts as stale: the row has never been
            # confirmed by this source, so it cannot have been mentioned in a
            # run that just completed.
            or_(
                CouponSource.last_seen_at.is_(None),
                CouponSource.last_seen_at < run_started_at,
            ),
        )
    )
    rows = list(result.scalars().all())
    for row in rows:
        row.status = as_status
    return len(rows)


@dataclass
class StoredClaim:
    """A prior source's claim, reconstructed from its `CouponSource` row.

    Deliberately *not* a `SourceClaim`. A `NormalizedOffer` is what a provider
    said this run; a `StoredClaim` is what it said on some earlier run, reduced
    to the fields we can trust to have been preserved. Rebuilding a full offer
    from storage would be a guess — `raw_data` is deliberately truncated — and
    guessing here would silently feed wrong values back into conflict resolution.
    """

    source: Source
    source_expires_at: datetime | None
    affiliate_url: str | None
    status: CouponSourceStatus


async def load_stored_claims(
    db: AsyncSession, coupon_id, *, only: tuple[CouponSourceStatus, ...] | None = None
) -> list[StoredClaim]:
    """Every source's stored position on one coupon, most-trusted first."""
    stmt = (
        select(CouponSource, Source)
        .join(Source, Source.id == CouponSource.source_id)
        .where(CouponSource.coupon_id == coupon_id)
    )
    if only:
        stmt = stmt.where(CouponSource.status.in_(only))
    claims = [
        StoredClaim(
            source=source,
            source_expires_at=row.source_expires_at,
            affiliate_url=row.affiliate_url,
            status=row.status,
        )
        for row, source in (await db.execute(stmt)).all()
    ]
    return sorted(claims, key=lambda c: c.source.precedence)


def resolve_from_stored(
    stored: list[StoredClaim],
    *,
    incoming_expires_at: datetime | None,
    incoming_destination: str,
    incoming_source: Source,
) -> tuple[datetime | None, str, list[str]]:
    """Fold a freshly-ingested offer into what the other sources already said.

    Re-implements the two rules from `resolve` against stored rows rather than
    live offers, because cross-run resolution cannot have the previous run's
    `NormalizedOffer` in hand. Returns the expiry, the destination URL, and the
    slugs of every source whose claim was overridden — the last is what makes the
    override visible in logs instead of silent.

    The two rules:

    * **Expiry — earliest wins.** Among the sources we trust most that actually
      stated a date, take the earliest. `incoming_expires_at=None` never erases a
      known date: providers omit expiry constantly and are rarely wrong about
      the ones they state.
    * **Destination — most trusted source that supplied one.** Not the most
      trusted source *period*: a priority-1 source that sent no URL must not
      leave the coupon pointing at a priority-50 fallback.
    """
    pool = [
        *stored,
        StoredClaim(
            source=incoming_source,
            source_expires_at=incoming_expires_at,
            affiliate_url=incoming_destination,
            status=CouponSourceStatus.active,
        ),
    ]
    ordered = sorted(pool, key=lambda c: c.source.precedence)

    # ---- Expiry ------------------------------------------------------------
    # Coerced, not assumed. `stored` dates come out of the database, and a
    # `DateTime(timezone=True)` column is only timezone-aware on Postgres; SQLite
    # returns naive. `incoming_expires_at` is always aware (normalize guarantees
    # it), so without this the `min()` below raises `TypeError: can't compare
    # offset-naive and offset-aware datetimes` — on the *second* run of a source,
    # when a stored claim finally exists, and never on the first. That asymmetry
    # is why it reads as a flake rather than a bug.
    stated = [c for c in ordered if c.source_expires_at is not None]
    expires_at: datetime | None = None
    if stated:
        best_priority = stated[0].source.priority
        tier = [
            ensure_utc(c.source_expires_at)
            for c in stated
            if c.source.priority == best_priority and c.source_expires_at is not None
        ]
        expires_at = min(tier)

    # ---- Destination -------------------------------------------------------
    with_url = [c for c in ordered if c.affiliate_url]
    destination = incoming_destination
    overridden: list[str] = []
    if with_url:
        winner = with_url[0]
        destination = winner.affiliate_url or incoming_destination
        overridden = [
            c.source.slug
            for c in ordered
            if c.source.priority > winner.source.priority and c.affiliate_url
        ]

    return expires_at, destination, overridden
