"""Merchant-affiliate program relationship.

A merchant (Store) can be enrolled in multiple affiliate programs across
different providers. This model captures that many-to-many relationship
with program-specific terms.

Example:
    Nike
     ├── Rakuten program (commission: 5%, cookie: 30 days, US only)
     ├── Awin program (commission: 4%, cookie: 30 days, US+GB)
     └── CJ program (commission: 6%, cookie: 7 days, US only)

The AffiliateRouter uses this table to decide which provider to send a
click to, based on country, commission, EPC, and tracking reliability.
"""

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    false,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.affiliate_provider import AffiliateProvider
    from app.models.store import Store


class CommissionType(str, enum.Enum):
    """How commission is calculated for this program."""

    percentage = "percentage"  # % of order value
    fixed = "fixed"  # fixed amount per conversion
    tiered = "tiered"  # varies by order value (stored in metadata)


class ProgramStatus(str, enum.Enum):
    """Lifecycle of a merchant's enrollment in a provider's program."""

    active = "active"  # Enrolled and receiving clicks
    pending = "pending"  # Application submitted, not yet approved
    paused = "paused"  # Temporarily disabled by operator
    terminated = "terminated"  # Permanently ended (by merchant or provider)


class AffiliateProgram(Base, UUIDPKMixin, TimestampMixin):
    """One merchant's enrollment in one provider's affiliate program.

    `external_program_id` is the provider's own identifier for this program.
    It is kept for API calls and reconciliation but is never exposed publicly.

    `commission_type` and `commission_value` define the payout structure.
    For tiered commissions, the tiers are stored in `metadata`.

    `cookie_duration_days` is how long after a click a conversion is credited.
    This varies by provider and sometimes by merchant within a provider.
    """

    __tablename__ = "affiliate_programs"

    __table_args__ = (
        # One program per (merchant, provider, country). A merchant may have
        # different terms per market, but not two active programs in the same
        # market on the same provider.
        UniqueConstraint("merchant_id", "provider_id", "country", name="uq_affiliate_program"),
        # "All active programs for this merchant" — the router's primary lookup.
        Index("ix_affiliate_programs_merchant_status", "merchant_id", "status"),
        # "All active programs on this provider" — for provider health dashboards.
        Index("ix_affiliate_programs_provider_status", "provider_id", "status"),
    )

    merchant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=False
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("affiliate_providers.id", ondelete="CASCADE"), nullable=False
    )

    # The provider's own program ID. Kept for API calls and reconciliation.
    external_program_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    status: Mapped[ProgramStatus] = mapped_column(
        Enum(ProgramStatus, name="program_status", native_enum=True),
        default=ProgramStatus.active,
        server_default=ProgramStatus.active.value,
        nullable=False,
    )

    # ISO 3166-1 alpha-2 market this program is valid for. Null = global.
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)

    commission_type: Mapped[CommissionType] = mapped_column(
        Enum(CommissionType, name="commission_type", native_enum=True),
        default=CommissionType.percentage,
        server_default=CommissionType.percentage.value,
        nullable=False,
    )
    commission_value: Mapped[float] = mapped_column(
        Numeric(10, 4, asdecimal=False),
        nullable=False,
        default=0.0,
        server_default="0.0",
    )

    # How long after a click a conversion is credited, in days.
    cookie_duration_days: Mapped[int] = mapped_column(
        Integer, default=30, server_default="30", nullable=False
    )

    # Whether this program supports deep links (direct-to-product URLs).
    deeplink_supported: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )

    # Whether this program supports sub-ID pass-through for attribution.
    tracking_supported: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )

    # When the merchant joined this program.
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the program terms were last verified with the provider.
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Provider-specific metadata (tiered commission rates, special terms, etc.)
    # Named `program_metadata` to avoid conflicting with SQLAlchemy's reserved
    # `metadata` attribute on Declarative classes.
    program_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    # Read-side conveniences
    merchant: Mapped["Store"] = relationship()
    provider: Mapped["AffiliateProvider"] = relationship()

    def __str__(self) -> str:
        return f"{self.merchant_id}:{self.provider_id}:{self.country or 'global'}"
