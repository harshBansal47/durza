"""Attribution model.

Attribution links a visitor's journey to a conversion. It captures:
- Which visitor and session
- Which coupon and merchant
- Which click led to the conversion
- Which provider and program
- First-touch and last-touch timestamps
- The attribution model used

The system supports multiple attribution models:
- `click_touch`: Last click before conversion gets credit (default for affiliate)
- `first_touch`: First interaction in the session gets credit
- `last_touch`: Last interaction before conversion gets credit

For affiliate revenue, `click_touch` is the primary model because affiliate
networks credit the last click. The other models are useful for understanding
the customer journey and for non-affiliate revenue sources.
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
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.click import ClickEvent
    from app.models.coupon import Coupon
    from app.models.store import Store


class AttributionModel(str, enum.Enum):
    """Which attribution model was used to assign credit."""

    click_touch = "click_touch"  # Last click before conversion (affiliate default)
    first_touch = "first_touch"  # First interaction in the session
    last_touch = "last_touch"  # Last interaction before conversion
    linear = "linear"  # Equal credit to all touches (future)


class Attribution(Base, UUIDPKMixin, TimestampMixin):
    """One attribution record linking a visitor journey to a conversion.

    `click_id` is the internal click reference (clickref) that was sent to
    the affiliate network. It is the primary key for matching conversions
    to clicks. When the network reports a conversion, it includes this
    clickref, which is how we know which click led to the sale.

    `attribution_model` records which model was used. For affiliate
    conversions, this is always `click_touch` because that is how
    affiliate networks credit conversions.

    `attribution_window` is the maximum time between click and conversion
    for the attribution to be valid. This matches the program's cookie
    duration.
    """

    __tablename__ = "attributions"

    __table_args__ = (
        # "All attributions for this click" — the primary lookup when a
        # conversion comes in from the network.
        Index("ix_attributions_click_id", "click_id"),
        # "All attributions for this visitor" — for journey analysis.
        Index("ix_attributions_visitor", "visitor_id"),
        # "All attributions for this session" — for session-level analysis.
        Index("ix_attributions_session", "session_id"),
        # "All attributions for this merchant" — for merchant-level analysis.
        Index("ix_attributions_merchant", "merchant_id"),
    )

    # ---- Visitor / session ----------------------------------------------
    visitor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ---- Coupon / merchant ----------------------------------------------
    coupon_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("coupons.id", ondelete="SET NULL"), nullable=True
    )
    merchant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("stores.id", ondelete="SET NULL"), nullable=True
    )

    # ---- Click linkage --------------------------------------------------
    # The internal click reference (clickref) sent to the network.
    # This is the primary key for matching conversions to clicks.
    click_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # The ClickEvent this attribution is based on (may be NULL if the
    # click happened before tracking was added).
    click_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("click_events.id", ondelete="SET NULL"), nullable=True
    )

    # ---- Provider / program ---------------------------------------------
    provider_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("affiliate_providers.id", ondelete="SET NULL"), nullable=True
    )
    program_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("affiliate_programs.id", ondelete="SET NULL"), nullable=True
    )

    # ---- Attribution model -----------------------------------------------
    attribution_model: Mapped[AttributionModel] = mapped_column(
        Enum(AttributionModel, name="attribution_model", native_enum=True),
        default=AttributionModel.click_touch,
        server_default=AttributionModel.click_touch.value,
        nullable=False,
    )

    # ---- Timestamps -----------------------------------------------------
    # When the first touch happened (first impression or click in this session).
    first_touch_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When the last touch happened (last click before conversion).
    last_touch_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When the conversion was recorded.
    converted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- Attribution window ----------------------------------------------
    # Maximum days between click and conversion for attribution to be valid.
    # Matches the program's cookie_duration_days.
    attribution_window: Mapped[int] = mapped_column(
        Integer, default=30, server_default="30", nullable=False
    )

    # ---- Metadata -------------------------------------------------------
    # Additional context (e.g. "matched by clickref", "matched by subid").
    attribution_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Read-side conveniences
    coupon: Mapped["Coupon | None"] = relationship()
    merchant: Mapped["Store | None"] = relationship()
    click_event: Mapped["ClickEvent | None"] = relationship()

    def __str__(self) -> str:
        return f"Attribution({self.click_id}, {self.attribution_model.value})"
