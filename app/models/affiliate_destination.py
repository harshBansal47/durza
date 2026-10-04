"""Tracked affiliate destination URLs.

An AffiliateDestination is a resolved, validated outbound URL for a
merchant-program pair. It is the final URL that the AffiliateRouter
redirects a visitor to after selecting a provider.

This model exists to:
1. Pre-validate destination URLs (no open redirects)
2. Cache resolved tracking URLs to avoid rebuilding them on every click
3. Provide an audit trail of what URL was actually used

The URL is built from the program's tracking template (if any) plus the
merchant's base destination. The clickref is added at click time, not
stored here.
"""

import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, ForeignKey, Index, String, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.affiliate_program import AffiliateProgram
    from app.models.store import Store


class DestinationStatus(str, enum.Enum):
    """Whether this destination is currently usable."""

    active = "active"
    paused = "paused"  # Temporarily disabled
    invalid = "invalid"  # Failed validation (bad URL, unreachable, etc.)


class AffiliateDestination(Base, UUIDPKMixin, TimestampMixin):
    """A validated outbound URL for a merchant-program pair.

    `destination_url` is the merchant's own URL (e.g. a product page or
    store homepage). `tracking_url` is the full URL with any provider-
    specific tracking parameters baked in (but NOT the clickref — that
    is added at click time).

    Never store raw provider credentials in this table. The tracking_url
    should only contain the base tracking parameters, not API keys.
    """

    __tablename__ = "affiliate_destinations"

    __table_args__ = (
        # One destination per (merchant, program). A merchant may have
        # different destinations per program (e.g. different deep links).
        Index(
            "ix_affiliate_destinations_merchant_program",
            "merchant_id",
            "program_id",
        ),
        # "All active destinations for this merchant" — for fallback routing.
        Index("ix_affiliate_destinations_merchant_status", "merchant_id", "status"),
    )

    merchant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=False
    )
    program_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("affiliate_programs.id", ondelete="CASCADE"), nullable=False
    )

    # The merchant's own URL (product page, store homepage, etc.)
    destination_url: Mapped[str] = mapped_column(String(1000), nullable=False)

    # The full tracking URL with provider-specific parameters (minus clickref).
    # Built from the program's tracking template + destination_url.
    tracking_url: Mapped[str] = mapped_column(String(2000), nullable=False)

    status: Mapped[DestinationStatus] = mapped_column(
        Enum(DestinationStatus, name="destination_status", native_enum=True),
        default=DestinationStatus.active,
        server_default=DestinationStatus.active.value,
        nullable=False,
    )

    # ISO 3166-1 alpha-2 market this destination is valid for. Null = global.
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)

    # Whether this is a deep link (direct to product/category) vs a landing page.
    deep_link: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )

    # Read-side conveniences
    merchant: Mapped["Store"] = relationship()
    program: Mapped["AffiliateProgram"] = relationship()

    def __str__(self) -> str:
        return f"{self.merchant_id}:{self.program_id}:{self.destination_url[:50]}"
