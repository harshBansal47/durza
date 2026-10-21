"""Coupon impression tracking.

Every meaningful coupon exposure is trackable. An impression is recorded
whenever a coupon card is rendered on a page — in a search result, on a
merchant page, on a category page, in a featured section, etc.

Impressions are the denominator for CTR calculations and the foundation
for ranking feedback. Without impression data, ranking is blind: you
cannot know whether a coupon at position #1 outperforms the same coupon
at position #5.

This model is deliberately separate from ClickEvent. A click without
an impression is a tracking bug; an impression without a click is a
ranking signal.

Rows are retained according to the analytics retention policy (separate
from financial/revenue records, which are never deleted).
"""

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import UUIDPKMixin

if TYPE_CHECKING:
    from app.models.coupon import Coupon
    from app.models.store import Store


class ImpressionType(str, enum.Enum):
    """Where the impression happened.

    This is the page context, not the coupon type. The same coupon can
    be impressioned on a merchant page, in search results, and on the
    homepage — each is a different impression context.
    """

    coupon_card = "coupon_card"
    coupon_detail = "coupon_detail"
    merchant_page = "merchant_page"
    category_page = "category_page"
    search_results = "search_results"
    homepage = "homepage"
    featured_section = "featured_section"
    deals = "deals"
    sponsored_section = "sponsored_section"


class CouponImpression(Base, UUIDPKMixin):
    """One coupon impression event.

    `position` is the 1-based rank of the coupon on the page. This is
    the single most important field for ranking analysis: it lets us
    answer "does position #1 get more clicks than position #3?" and
    "does the coupon at position #1 convert better?"

    `ranking_version` identifies which ranking algorithm version produced
    this ordering. This is essential for A/B testing ranking changes.

    `ranking_score` is the score the ranking algorithm assigned to this
    coupon at this position. This lets us analyze whether higher-scored
    coupons actually perform better.

    `sponsored` distinguishes organic from sponsored impressions. Sponsored
    impressions are paid placements and must be analyzed separately from
    organic ones.
    """

    __tablename__ = "coupon_impressions"

    __table_args__ = (
        # "All impressions for this coupon in a time window" — CTR calculation.
        Index("ix_coupon_impressions_coupon_created", "coupon_id", "created_at"),
        # "All impressions on this page type in a time window" — page-level CTR.
        Index("ix_coupon_impressions_page_type_created", "page_type", "created_at"),
        # "All impressions for this merchant" — merchant-level analytics.
        Index("ix_coupon_impressions_merchant_created", "merchant_id", "created_at"),
        # "All impressions from this traffic source" — source-level analytics.
        Index("ix_coupon_impressions_source", "source"),
    )

    coupon_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("coupons.id", ondelete="CASCADE"), nullable=False
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=False
    )

    # ---- Visitor / session ----------------------------------------------
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    visitor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ---- Page context ---------------------------------------------------
    page_type: Mapped[ImpressionType] = mapped_column(
        Enum(ImpressionType, name="impression_type", native_enum=True),
        nullable=False,
    )
    page_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ---- Ranking --------------------------------------------------------
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    ranking_version: Mapped[str] = mapped_column(
        String(20), default="v1", server_default="v1", nullable=False
    )
    ranking_score: Mapped[float | None] = mapped_column(nullable=True)

    # ---- Traffic source -------------------------------------------------
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    referrer: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # ---- Device / geography ---------------------------------------------
    device_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)

    # ---- Sponsored flag -------------------------------------------------
    sponsored: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )

    # ---- Timestamp ------------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Read-side conveniences
    coupon: Mapped["Coupon"] = relationship()
    merchant: Mapped["Store"] = relationship()

    def __str__(self) -> str:
        return f"Impression({self.coupon_id}, pos={self.position}, {self.page_type.value})"
