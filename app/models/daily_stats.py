"""Daily analytics aggregations.

Raw events (clicks, impressions, conversions) are the source of truth,
but expensive to aggregate on every admin request. These models hold
daily pre-aggregated stats, updated by a background job.

Each model follows the same pattern: one row per (date, entity) pair
with all the metrics computed from the raw events for that day.
"""

import uuid
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Date, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.affiliate_provider import AffiliateProvider
    from app.models.coupon import Coupon
    from app.models.store import Store


class DailyCouponStats(Base, UUIDPKMixin, TimestampMixin):
    """Daily aggregate stats for one coupon.

    Updated by a nightly background job. Not the source of truth —
    the raw events (ClickEvent, CouponImpression, Conversion) are.
    """

    __tablename__ = "daily_coupon_stats"

    __table_args__ = (
        UniqueConstraint("date", "coupon_id", name="uq_daily_coupon_stats"),
        Index("ix_daily_coupon_stats_coupon_date", "coupon_id", "date"),
    )

    date: Mapped[date] = mapped_column(Date, nullable=False)
    coupon_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("coupons.id", ondelete="CASCADE"), nullable=False
    )

    impressions: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    clicks: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    conversions: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    revenue: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False),
        default=0.0,
        server_default="0.0",
        nullable=False,
    )
    confirmed_revenue: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False),
        default=0.0,
        server_default="0.0",
        nullable=False,
    )

    currency: Mapped[str] = mapped_column(
        String(3), default="USD", server_default="USD", nullable=False
    )

    ctr: Mapped[float | None] = mapped_column(Numeric(10, 4, asdecimal=False), nullable=True)
    conversion_rate: Mapped[float | None] = mapped_column(
        Numeric(10, 4, asdecimal=False), nullable=True
    )
    epc: Mapped[float | None] = mapped_column(Numeric(10, 4, asdecimal=False), nullable=True)

    coupon: Mapped["Coupon"] = relationship()

    def __str__(self) -> str:
        return f"DailyCouponStats({self.date}, {self.coupon_id})"


class DailyMerchantStats(Base, UUIDPKMixin, TimestampMixin):
    """Daily aggregate stats for one merchant."""

    __tablename__ = "daily_merchant_stats"

    __table_args__ = (
        UniqueConstraint("date", "merchant_id", name="uq_daily_merchant_stats"),
        Index("ix_daily_merchant_stats_merchant_date", "merchant_id", "date"),
    )

    date: Mapped[date] = mapped_column(Date, nullable=False)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=False
    )

    impressions: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    clicks: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    conversions: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    revenue: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False),
        default=0.0,
        server_default="0.0",
        nullable=False,
    )
    confirmed_revenue: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False),
        default=0.0,
        server_default="0.0",
        nullable=False,
    )

    currency: Mapped[str] = mapped_column(
        String(3), default="USD", server_default="USD", nullable=False
    )

    ctr: Mapped[float | None] = mapped_column(Numeric(10, 4, asdecimal=False), nullable=True)
    conversion_rate: Mapped[float | None] = mapped_column(
        Numeric(10, 4, asdecimal=False), nullable=True
    )
    epc: Mapped[float | None] = mapped_column(Numeric(10, 4, asdecimal=False), nullable=True)

    merchant: Mapped["Store"] = relationship()

    def __str__(self) -> str:
        return f"DailyMerchantStats({self.date}, {self.merchant_id})"


class DailyProviderStats(Base, UUIDPKMixin, TimestampMixin):
    """Daily aggregate stats for one affiliate provider."""

    __tablename__ = "daily_provider_stats"

    __table_args__ = (
        UniqueConstraint("date", "provider_id", name="uq_daily_provider_stats"),
        Index("ix_daily_provider_stats_provider_date", "provider_id", "date"),
    )

    date: Mapped[date] = mapped_column(Date, nullable=False)
    provider_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("affiliate_providers.id", ondelete="CASCADE"), nullable=False
    )

    clicks: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    conversions: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    commission: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False),
        default=0.0,
        server_default="0.0",
        nullable=False,
    )
    confirmed_commission: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False),
        default=0.0,
        server_default="0.0",
        nullable=False,
    )

    currency: Mapped[str] = mapped_column(
        String(3), default="USD", server_default="USD", nullable=False
    )

    conversion_rate: Mapped[float | None] = mapped_column(
        Numeric(10, 4, asdecimal=False), nullable=True
    )
    epc: Mapped[float | None] = mapped_column(Numeric(10, 4, asdecimal=False), nullable=True)

    provider: Mapped["AffiliateProvider"] = relationship()

    def __str__(self) -> str:
        return f"DailyProviderStats({self.date}, {self.provider_id})"


class DailyTrafficStats(Base, UUIDPKMixin, TimestampMixin):
    """Daily aggregate stats by traffic source."""

    __tablename__ = "daily_traffic_stats"

    __table_args__ = (
        UniqueConstraint("date", "source", name="uq_daily_traffic_stats"),
        Index("ix_daily_traffic_stats_date", "date"),
    )

    date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)

    impressions: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    clicks: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    conversions: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    revenue: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False),
        default=0.0,
        server_default="0.0",
        nullable=False,
    )

    currency: Mapped[str] = mapped_column(
        String(3), default="USD", server_default="USD", nullable=False
    )

    ctr: Mapped[float | None] = mapped_column(Numeric(10, 4, asdecimal=False), nullable=True)
    conversion_rate: Mapped[float | None] = mapped_column(
        Numeric(10, 4, asdecimal=False), nullable=True
    )
    epc: Mapped[float | None] = mapped_column(Numeric(10, 4, asdecimal=False), nullable=True)

    def __str__(self) -> str:
        return f"DailyTrafficStats({self.date}, {self.source})"


class DailyContentStats(Base, UUIDPKMixin, TimestampMixin):
    """Daily aggregate stats for one content piece (guide, etc.)."""

    __tablename__ = "daily_content_stats"

    __table_args__ = (
        UniqueConstraint("date", "content_ref", "content_type", name="uq_daily_content_stats"),
        Index("ix_daily_content_stats_ref_date", "content_ref", "date"),
    )

    date: Mapped[date] = mapped_column(Date, nullable=False)
    content_ref: Mapped[str] = mapped_column(String(80), nullable=False)
    content_type: Mapped[str] = mapped_column(String(30), nullable=False)

    views: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    scroll_25: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    scroll_50: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    scroll_75: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    scroll_100: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    engaged_time_sum_ms: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    engaged_time_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    outbound_clicks: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    conversions: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    confirmed_revenue: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False), default=0.0, server_default="0.0", nullable=False
    )
    currency: Mapped[str] = mapped_column(
        String(3), default="USD", server_default="USD", nullable=False
    )
