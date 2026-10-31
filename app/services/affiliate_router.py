"""Deciding where a click actually goes, server-side.

When Rakuten, Awin, CJ and Admitad all advertise the same code at the same
merchant, `CouponSource` holds one row per provider and each row carries that
provider's own affiliate URL. A shopper clicking one card has to be sent to
exactly one of them. This module is the only thing that decides which.

Two rules, and the reasons they are the rules:

* **Precedence decides, nothing else does.** The winner is the highest-priority
  source that still asserts this coupon exists and has a link for it — the same
  `Source.precedence` key conflict resolution uses, so the metadata a shopper
  reads and the link they land on come from the same source whenever possible.
* **A broken ingest API is not a broken link.** `Source.api_status` is not
  consulted. It records whether *we* can read a provider's catalogue, and a
  provider whose API has been returning 500s for a week still hands out working
  deep links. Retiring its links on that basis would mean a CJ API key expiring
  takes CJ's links offline even though the merchant's server never noticed.

What is deliberately *not* here, because it was invented rather than measured:
weighted scores over historical EPC, conversion rate, commission rate and
"tracking quality". Every one of those inputs was a number we made up —
estimated clicks as `conversions * 20`, a capability matrix invented from a
provider name — so the "optimised" routing was arithmetic on noise, and it would
have sent shoppers to whichever network flattered us most rather than the one
their coupon came from. If real per-network performance data ever exists, the
honest place for it is a new, lower-precedence term here that only breaks ties
*between equal-priority sources*, never outranks an operator's stated trust.

## Provider identity stays in the routing layer

The result exposes `source_slug` for logs and the selected network internally
so the redirect can use the matching click-reference parameter. Callers must
not serialise either: a shopper's browser can see which network sent them,
which is a commercial detail we have no reason to publish.
"""

import enum
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.coupon import Coupon
from app.models.coupon_source import CouponSource, CouponSourceStatus
from app.models.source import Source
from app.services.affiliate import network_for_source_kind

logger = logging.getLogger("durza.routing")

#: Source statuses that still mean "this provider currently asserts the coupon
#: exists", so its link is a legitimate place to send a shopper.
#:
#: `expired` is in here on purpose. `Coupon` is only expired once the evidence
#: says so; one provider calling a code dead does not retire a coupon the others
#: are still selling. When the canonical coupon is being shown, this provider's
#: deep link is still the best URL we hold for it.
#:
#: `deleted`, `inactive` and `failed` are out: those say the provider stopped
#: listing the offer or that we could not read it, and in the last case we have
#: no idea whether the offer is even real. `deleted` in particular only ever
#: follows a *completed* run (see `conflicts.mark_stale_for_source`), so it is
#: genuine evidence — which is exactly why it disqualifies.
ELIGIBLE_SOURCE_STATUSES: tuple[CouponSourceStatus, ...] = (
    CouponSourceStatus.active,
    CouponSourceStatus.expired,
)


class RoutingDecision(str, enum.Enum):
    """Which rule produced the URL, for logs and the admin view."""

    source = "source"  # a CouponSource.affiliate_url was chosen
    fallback = "fallback"  # no source was eligible; Coupon.destination_url


@dataclass(frozen=True)
class RoutedDestination:
    """Where to send one click, and why.

    `url` is a bare affiliate URL. Attaching the click reference is the caller's
    job (`app.services.affiliate.build_outbound_url`), using the selected
    source's network when it is known and the store configuration otherwise.
    """

    url: str
    decision: RoutingDecision
    source_slug: str | None = None
    source_status: CouponSourceStatus | None = None
    network: str | None = None
    reason: str = ""
    #: How many source rows were eligible. Non-zero with a `fallback` decision
    #: is impossible by construction; it is here so a log line can distinguish
    #: "nothing to choose from" from "chose among four".
    candidates: int = 0


def _covers_country(source: Source, country: str | None) -> bool:
    """Is this source authoritative for `country`?

    `Source.countries` empty means "no opinion", and is treated as eligible
    everywhere. That is the right default in the direction it errs: a source
    with no market restriction is far more likely to be a global feed than a
    mislabelled one, and excluding it would throw away good links on a technicality
    an operator never set.
    """
    if not country:
        return True
    markets = [c.upper() for c in (source.countries or []) if c]
    return not markets or country.upper() in markets


async def eligible_sources(db: AsyncSession, coupon: Coupon) -> list[tuple[CouponSource, Source]]:
    """This coupon's source rows that could win, most trusted first.

    Fetched by the indexed `(coupon_id, status)` lookup and then ordered in
    Python by `Source.precedence`. Ordering in SQL would mean writing the sort
    key twice; a coupon has a handful of providers, so the sort costs nothing
    and keeping one implementation is worth far more.
    """
    stmt = (
        select(CouponSource, Source)
        .join(Source, Source.id == CouponSource.source_id)
        .where(
            CouponSource.coupon_id == coupon.id,
            CouponSource.status.in_(ELIGIBLE_SOURCE_STATUSES),
            CouponSource.affiliate_url.is_not(None),
            # An operator switching a source off is the one signal strong enough
            # to stop using its links. Health is not — see the module docstring.
            Source.is_enabled.is_(True),
        )
    )
    rows = [(row, source) for row, source in (await db.execute(stmt)).all()]

    eligible = [(row, source) for row, source in rows if _covers_country(source, coupon.country)]

    # `active` outranks `expired` ahead of the source ordering. A deep link from
    # a provider that still calls the coupon live is a better destination than
    # one from a provider that has already expired it, even when the expired
    # source is nominally higher priority — otherwise a shopper is pointed at a
    # link we already know the merchant considers dead.
    return sorted(
        eligible,
        key=lambda pair: (
            0 if pair[0].status is CouponSourceStatus.active else 1,
            pair[1].precedence,
        ),
    )


async def route_coupon(db: AsyncSession, coupon: Coupon) -> RoutedDestination:
    """Pick the URL for one click on one coupon. The rules are on `_route`.

    Logged at DEBUG: one line per click is far too many for INFO, but when a
    coupon sends people somewhere unexpected this is the line that says why.
    """
    routed = await _route(db, coupon)
    logger.debug(
        "click routed",
        extra={
            "coupon_id": str(coupon.id),
            "decision": routed.decision.value,
            "source": routed.source_slug,
            "candidates": routed.candidates,
            "reason": routed.reason,
        },
    )
    return routed


async def _route(db: AsyncSession, coupon: Coupon) -> RoutedDestination:
    """Pick the URL for one click on one coupon.

    Always returns something: the denormalized `Coupon.destination_url` is the
    fallback, so a hand-created coupon with no source rows still redirects.
    Callers that need to refuse the click entirely should check `is_active`
    first — routing is not an entitlement check.
    """
    candidates = await eligible_sources(db, coupon)

    if not candidates:
        return RoutedDestination(
            url=coupon.destination_url,
            decision=RoutingDecision.fallback,
            reason=(
                "no source row for this coupon carries a usable link; "
                "used the resolved destination_url"
            ),
        )

    row, source = candidates[0]
    if len(candidates) == 1:
        reason = "only one source has a link for this coupon"
    else:
        reason = (
            f"highest-precedence of {len(candidates)} sources with a link "
            f"(priority {source.priority}, confidence {source.confidence:g})"
        )
    if row.status is CouponSourceStatus.expired:
        # Worth saying out loud: we are preferring a live source and falling
        # back to an expired one, or there was nothing else.
        reason += "; this source has expired the coupon and is the best available"

    return RoutedDestination(
        url=row.affiliate_url or coupon.destination_url,
        decision=RoutingDecision.source,
        source_slug=source.slug,
        source_status=row.status,
        network=network_for_source_kind(source.kind.value),
        reason=reason,
        candidates=len(candidates),
    )
