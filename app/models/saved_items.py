import enum
import uuid

from sqlalchemy import (
    Boolean,
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin


class SavedStore(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "saved_stores"
    __table_args__ = (UniqueConstraint("user_id", "store_id", name="uq_saved_store"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    store_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), index=True, nullable=False
    )


class SavedCoupon(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "saved_coupons"
    __table_args__ = (UniqueConstraint("user_id", "coupon_id", name="uq_saved_coupon"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    coupon_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("coupons.id", ondelete="CASCADE"), index=True, nullable=False
    )


class NotificationPreference(Base, UUIDPKMixin, TimestampMixin):
    """One row per user: whether email updates about saved items are enabled."""

    __tablename__ = "notification_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    email_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class AlertKind(str, enum.Enum):
    # Deal alerts are derived from saved items; no per-user alert history is kept.
    new_offer = "new_offer"  # a saved store has a new live offer
    confirmed = "confirmed"  # a saved offer was confirmed working
    ending_soon = "ending_soon"  # a saved offer is approaching its expiry
    ended = "ended"  # a saved offer expired, or stopped working
