"""Earnings Per Click, the number the ranking has to optimise against.

EPC = confirmed net commission / outbound clicks, over a rolling window.
Two grouping levels matter:

* **store** — the operator view: which merchants earn, so rates/pages can be
  renegotiated or dropped.
* **coupon** — the ranking input: which codes make us money, so the
  "Recommended" order can weight them.

Only clicks that could earn money count (sponsored `ad_id` clicks and
bot-flagged clicks are excluded), and only confirmed commission does —
pending and reversed amounts never move a ranking.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.affiliate_revenue import AffiliateRevenue, RevenueStatus
from app.models.click import ClickEvent
from app.models.conversion import Conversion

DEFAULT_WINDOW_DAYS = 30


def _click_counts(group_col):
    return (
        select(group_col, func.count())
        .select_from(ClickEvent)
        .where(
            ClickEvent.ad_id.is_(None),
            ClickEvent.is_bot.is_(False),
            group_col.is_not(None),
        )
        .group_by(group_col)
    )


def _commission_sums(group_col):
    return (
        select(
            group_col,
            AffiliateRevenue.currency,
            func.coalesce(func.sum(AffiliateRevenue.net_commission), 0.0),
        )
        .select_from(Conversion)
        .join(ClickEvent, ClickEvent.id == Conversion.click_id)
        .join(AffiliateRevenue, AffiliateRevenue.conversion_id == Conversion.id)
        .where(
            AffiliateRevenue.status == RevenueStatus.confirmed,
            ClickEvent.ad_id.is_(None),
            ClickEvent.is_bot.is_(False),
            group_col.is_not(None),
        )
        .group_by(group_col, AffiliateRevenue.currency)
    )


async def _epc_by(group_col, db: AsyncSession, *, days: int) -> dict[object, float]:
    """EPC keyed by the grouping column. Multi-currency sums are not mixed:
    a group's EPC is returned in its dominant (largest) currency's amount."""
    since = datetime.now(UTC) - timedelta(days=days)
    counts_q = _click_counts(group_col).where(ClickEvent.created_at >= since)
    commission_q = _commission_sums(group_col).where(Conversion.created_at >= since)

    clicks = {key: count for key, count in (await db.execute(counts_q)).all()}

    # key -> [(currency, total)] so we can pick one currency per key.
    commission: dict[object, list[tuple[str, float]]] = {}
    for key, currency, total in (await db.execute(commission_q)).all():
        commission.setdefault(key, []).append((currency, float(total)))

    out: dict[object, float] = {}
    for key, count in clicks.items():
        if count <= 0:
            continue
        totals = commission.get(key, [])
        amount = max((total for _, total in totals), default=0.0)
        out[key] = amount / count
    return out


async def epc_by_store(
    db: AsyncSession, *, days: int = DEFAULT_WINDOW_DAYS
) -> dict[uuid.UUID, float]:
    return await _epc_by(ClickEvent.store_id, db, days=days)  # type: ignore[return-value]


async def epc_by_coupon(
    db: AsyncSession, *, days: int = DEFAULT_WINDOW_DAYS
) -> dict[uuid.UUID, float]:
    return await _epc_by(ClickEvent.coupon_id, db, days=days)  # type: ignore[return-value]
