"""Multi-source revenue event tracking.

The RevenueEvent model captures ALL revenue, not just affiliate commission.
This allows the system to track:
- AFFILIATE — commission from affiliate networks
- SPONSORED_PLACEMENT — payment from merchants for sponsored placements
- MERCHANT_SUBSCRIPTION — subscription fees from merchants
- DIRECT_CAMPAIGN — direct campaign payments
- OTHER — any other revenue source

Affiliate-specific details remain in the AffiliateRevenue model. This
model is the unified view of all revenue, enabling total revenue
reporting regardless of source.
"""

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.store import Store


class RevenueSourceType(str, enum.Enum):
    """The source of the revenue."""

    affiliate = "affiliate"
    sponsored_placement = "sponsored_placement"
    merchant_subscription = "merchant_subscription"
    direct_campaign = "direct_campaign"
    other = "other"


class RevenueEvent(Base, UUIDPKMixin, TimestampMixin):
    """One revenue event from any source.

    `source_type` and `source_id` link to the specific source record
    (e.g. an AffiliateRevenue ID for affiliate revenue). The source_id
    is a string rather than a UUID because different source types have
    different ID formats.

    `amount` and `currency` are the original amounts. Normalized amounts
    are stored separately when currency conversion is needed.

    `status` tracks the revenue through its lifecycle: pending → confirmed
    → reversed.
    """

    __tablename__ = "revenue_events"

    __table_args__ = (
        # "All revenue events for this merchant"
        Index("ix_revenue_events_merchant_created", "merchant_id", "created_at"),
        # "All revenue events by source type"
        Index("ix_revenue_events_source_type", "source_type"),
        # "Revenue events by status"
        Index("ix_revenue_events_status", "status"),
    )

    # ---- Source ----------------------------------------------------------
    source_type: Mapped[RevenueSourceType] = mapped_column(
        Enum(RevenueSourceType, name="revenue_source_type", native_enum=True),
        nullable=False,
    )
    source_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ---- Merchant --------------------------------------------------------
    merchant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("stores.id", ondelete="SET NULL"), nullable=True
    )

    # ---- Amounts ---------------------------------------------------------
    amount: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False),
        nullable=False,
        default=0.0,
        server_default="0.0",
    )
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="USD", server_default="USD"
    )

    # ---- Status ----------------------------------------------------------
    status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending", nullable=False
    )

    # ---- Dates -----------------------------------------------------------
    event_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confirmed_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- Normalized revenue (optional) -----------------------------------
    normalized_amount: Mapped[float | None] = mapped_column(
        Numeric(12, 2, asdecimal=False), nullable=True
    )
    normalized_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    exchange_rate: Mapped[float | None] = mapped_column(
        Numeric(12, 6, asdecimal=False), nullable=True
    )
    exchange_rate_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Read-side convenience
    merchant: Mapped["Store | None"] = relationship()

    def __str__(self) -> str:
        return f"RevenueEvent({self.source_type.value}, {self.amount} {self.currency})"
