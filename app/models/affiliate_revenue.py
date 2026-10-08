"""Affiliate revenue tracking.

A separate revenue record for each commission earned. This is distinct
from the Conversion model (which tracks the network's view of the
transaction) and the RevenueEvent model (which tracks all revenue sources).

The AffiliateRevenue model captures:
- Gross commission (what the network says we earned)
- Adjustments (corrections, bonuses, penalties)
- Net commission (what we actually expect to be paid)
- Currency (never mixed; normalized separately)
- Status (pending/confirmed/reversed)

Revenue records are never deleted, even when analytics events expire.
They are financial records and must be retained for accounting.
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
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.affiliate_program import AffiliateProgram
    from app.models.affiliate_provider import AffiliateProvider
    from app.models.store import Store


class RevenueStatus(str, enum.Enum):
    """Lifecycle of a revenue record.

    `pending` — the network reported the commission but it is not yet
      confirmed (e.g. within the return window).
    `confirmed` — the commission is locked in and will be paid.
    `reversed` — the commission was reversed (e.g. due to a refund,
      chargeback, or network adjustment).
    """

    pending = "pending"
    confirmed = "confirmed"
    reversed = "reversed"


class AffiliateRevenue(Base, UUIDPKMixin, TimestampMixin):
    """One commission earned from an affiliate conversion.

    `gross_commission` is the amount the network reported before any
    adjustments. `adjustments` captures corrections (positive or
    negative). `net_commission` is the final amount expected to be paid.

    `currency` is the ISO 4217 code. Amounts are never mixed across
    currencies. Normalized revenue (if needed) is stored separately
    with the exchange rate and timestamp.

    `conversion_date` is when the sale happened. `reported_date` is
    when the network reported it. `confirmed_date` is when the
    commission was locked in.
    """

    __tablename__ = "affiliate_revenues"

    __table_args__ = (
        # "All revenue for this merchant in a time window"
        Index("ix_affiliate_revenues_merchant_created", "merchant_id", "created_at"),
        # "All revenue for this provider in a time window"
        Index("ix_affiliate_revenues_provider_created", "provider_id", "created_at"),
        # "All revenue for this program in a time window"
        Index("ix_affiliate_revenues_program_created", "program_id", "created_at"),
        # "Revenue by status" — for reconciliation
        Index("ix_affiliate_revenues_status", "status"),
    )

    # ---- Links ----------------------------------------------------------
    conversion_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversions.id", ondelete="SET NULL"), nullable=True
    )
    merchant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("stores.id", ondelete="SET NULL"), nullable=True
    )
    provider_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("affiliate_providers.id", ondelete="SET NULL"), nullable=True
    )
    program_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("affiliate_programs.id", ondelete="SET NULL"), nullable=True
    )

    # ---- Amounts ---------------------------------------------------------
    gross_commission: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False),
        nullable=False,
        default=0.0,
        server_default="0.0",
    )
    adjustments: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False),
        nullable=False,
        default=0.0,
        server_default="0.0",
    )
    net_commission: Mapped[float] = mapped_column(
        Numeric(12, 2, asdecimal=False),
        nullable=False,
        default=0.0,
        server_default="0.0",
    )
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="USD", server_default="USD"
    )

    # ---- Status ----------------------------------------------------------
    status: Mapped[RevenueStatus] = mapped_column(
        Enum(RevenueStatus, name="revenue_status", native_enum=True),
        default=RevenueStatus.pending,
        server_default=RevenueStatus.pending.value,
        nullable=False,
    )

    # ---- Dates -----------------------------------------------------------
    conversion_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reported_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confirmed_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- Normalized revenue (optional) -----------------------------------
    # When revenue needs to be compared across currencies, the normalized
    # amount and exchange rate are stored here. Never fabricate rates.
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

    # ---- Metadata --------------------------------------------------------
    # Reason for reversal, adjustment details, etc.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Read-side conveniences
    merchant: Mapped["Store | None"] = relationship()
    provider: Mapped["AffiliateProvider | None"] = relationship()
    program: Mapped["AffiliateProgram | None"] = relationship()

    def __str__(self) -> str:
        return f"Revenue({self.gross_commission} {self.currency}, {self.status.value})"
