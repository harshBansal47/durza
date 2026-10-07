"""Earnings-per-click reporting.

EPC is the number that answers "is this merchant/source worth our traffic":
commission earned per outbound click. Only clicks that could earn money count
— sponsored-placement clicks (`ad_id` set) and bot-flagged clicks are excluded
from both the click base and the commission total, and commission is only
counted once it is confirmed by the network (never pending or reversed).
"""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_role
from app.models.affiliate_revenue import AffiliateRevenue, RevenueStatus
from app.models.click import ClickEvent
from app.models.conversion import Conversion
from app.models.store import Store
from app.models.user import Role, User

router = APIRouter(prefix="/earnings", tags=["earnings"])


def _clicks_stmt(*, group_col, since: datetime):
    return (
        select(group_col, func.count())
        .select_from(ClickEvent)
        .where(
            ClickEvent.ad_id.is_(None),
            ClickEvent.is_bot.is_(False),
            ClickEvent.created_at >= since,
            group_col.is_not(None),
        )
        .group_by(group_col)
    )


def _revenue_stmt(*, group_col, since: datetime):
    return (
        select(
            group_col,
            AffiliateRevenue.currency,
            func.coalesce(func.sum(AffiliateRevenue.net_commission), 0.0),
            func.count(Conversion.id),
        )
        .select_from(Conversion)
        .join(ClickEvent, ClickEvent.id == Conversion.click_id)
        .join(AffiliateRevenue, AffiliateRevenue.conversion_id == Conversion.id)
        .where(
            AffiliateRevenue.status == RevenueStatus.confirmed,
            ClickEvent.ad_id.is_(None),
            ClickEvent.is_bot.is_(False),
            Conversion.created_at >= since,
            group_col.is_not(None),
        )
        .group_by(group_col, AffiliateRevenue.currency)
    )


@router.get("/epc")
async def earnings_per_click(
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role(Role.admin)),
) -> dict:
    since = datetime.now(UTC) - timedelta(days=days)

    # ---- by store ----
    clicks_by_store = dict(
        (await db.execute(_clicks_stmt(group_col=ClickEvent.store_id, since=since))).all()
    )
    stores = {s.id: s for s in (await db.execute(select(Store))).scalars().all()}
    rev_rows = (await db.execute(_revenue_stmt(group_col=ClickEvent.store_id, since=since))).all()
    rev_by_store: dict[uuid.UUID, dict[str | None, dict]] = {}
    for store_id, currency, total, conversions in rev_rows:
        rev_by_store.setdefault(store_id, {})[currency] = {
            "commission": round(float(total), 2),
            "conversions": conversions,
        }
    store_ids = set(clicks_by_store) | set(rev_by_store)
    by_store = []
    for store_id in store_ids:
        clicks = clicks_by_store.get(store_id, 0)
        currencies = rev_by_store.get(store_id) or {None: {"commission": 0.0, "conversions": 0}}
        for currency, agg in currencies.items():
            by_store.append(
                {
                    "store_id": str(store_id),
                    "store_name": stores[store_id].name if store_id in stores else None,
                    "currency": currency,
                    "clicks": clicks,
                    "conversions": agg["conversions"],
                    "commission": agg["commission"],
                    "epc": round(agg["commission"] / clicks, 4) if clicks else 0.0,
                }
            )
    by_store.sort(key=lambda row: row["commission"], reverse=True)

    # ---- by source (click surface, e.g. "coupon-page") ----
    clicks_by_src = dict(
        (await db.execute(_clicks_stmt(group_col=ClickEvent.src, since=since))).all()
    )
    rev_rows = (await db.execute(_revenue_stmt(group_col=ClickEvent.src, since=since))).all()
    rev_by_src: dict[str, dict[str | None, dict]] = {}
    for src, currency, total, conversions in rev_rows:
        rev_by_src.setdefault(src, {})[currency] = {
            "commission": round(float(total), 2),
            "conversions": conversions,
        }
    srcs = set(clicks_by_src) | set(rev_by_src)
    by_source = []
    for src in srcs:
        clicks = clicks_by_src.get(src, 0)
        currencies = rev_by_src.get(src) or {None: {"commission": 0.0, "conversions": 0}}
        for currency, agg in currencies.items():
            by_source.append(
                {
                    "source": src,
                    "currency": currency,
                    "clicks": clicks,
                    "conversions": agg["conversions"],
                    "commission": agg["commission"],
                    "epc": round(agg["commission"] / clicks, 4) if clicks else 0.0,
                }
            )
    by_source.sort(key=lambda row: row["commission"], reverse=True)

    # ---- by coupon ----
    clicks_by_coupon = dict(
        (await db.execute(_clicks_stmt(group_col=ClickEvent.coupon_id, since=since))).all()
    )
    rev_rows = (await db.execute(_revenue_stmt(group_col=ClickEvent.coupon_id, since=since))).all()
    rev_by_coupon: dict[uuid.UUID, dict[str | None, dict]] = {}
    for coupon_id, currency, total, conversions in rev_rows:
        rev_by_coupon.setdefault(coupon_id, {})[currency] = {
            "commission": round(float(total), 2),
            "conversions": conversions,
        }
    coupon_ids = set(clicks_by_coupon) | set(rev_by_coupon)
    by_coupon = []
    for coupon_id in coupon_ids:
        clicks = clicks_by_coupon.get(coupon_id, 0)
        currencies = rev_by_coupon.get(coupon_id) or {None: {"commission": 0.0, "conversions": 0}}
        for currency, agg in currencies.items():
            by_coupon.append(
                {
                    "coupon_id": str(coupon_id),
                    "currency": currency,
                    "clicks": clicks,
                    "conversions": agg["conversions"],
                    "commission": agg["commission"],
                    "epc": round(agg["commission"] / clicks, 4) if clicks else 0.0,
                }
            )
    by_coupon.sort(key=lambda row: row["commission"], reverse=True)

    return {"days": days, "by_store": by_store, "by_source": by_source, "by_coupon": by_coupon}
