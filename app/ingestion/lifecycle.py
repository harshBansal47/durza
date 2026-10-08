"""Keep coupon states honest over time — no scheduled worker required, just a
script you run on a cron (scripts/refresh_coupons.py)."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.coupon import Coupon, CouponStatus


async def expire_stale_coupons(db: AsyncSession) -> int:
    """Mark active coupons whose expiry has passed as expired. Returns the count."""
    now = datetime.now(UTC)
    result = await db.execute(
        select(Coupon).where(
            Coupon.status == CouponStatus.active,
            Coupon.expires_at.is_not(None),
            Coupon.expires_at <= now,
        )
    )
    stale = list(result.scalars().all())
    for coupon in stale:
        coupon.status = CouponStatus.expired
        coupon.is_active = False
        coupon.failure_reason = "expired"
    if stale:
        await db.commit()
    return len(stale)
