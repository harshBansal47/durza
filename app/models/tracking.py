import enum
import uuid

from sqlalchemy import Boolean, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint
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


class TrackedProduct(Base, UUIDPKMixin, TimestampMixin):
    """A user watching a product, optionally with a target price that fires alerts."""

    __tablename__ = "tracked_products"
    __table_args__ = (UniqueConstraint("user_id", "product_id", name="uq_tracked_product"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True, nullable=False
    )
    target_price: Mapped[float | None] = mapped_column(Float(), nullable=True)


class NotificationPreference(Base, UUIDPKMixin, TimestampMixin):
    """One row per user: which channels are on, and where each channel reaches them."""

    __tablename__ = "notification_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    email_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    telegram_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    push_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # JSON-encoded push subscription (endpoint + keys), as the browser Push API returns it.
    push_subscription: Mapped[str | None] = mapped_column(Text, nullable=True)


class AlertKind(str, enum.Enum):
    price_drop = "price_drop"
    coupon_appeared = "coupon_appeared"
    target_met = "target_met"  # effective price fell below the user's target


class AlertEvent(Base, UUIDPKMixin, TimestampMixin):
    """Every alert we actually sent (or attempted) — history + dedupe guard."""

    __tablename__ = "alert_events"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    tracked_product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tracked_products.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kind: Mapped[AlertKind] = mapped_column(
        Enum(AlertKind, name="alert_kind", native_enum=True), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Whether the channel actually accepted the send. Every attempt is recorded
    # — otherwise "I never got that email" is unfalsifiable — but a failed send
    # should be visibly different from a delivered one in the history.
    delivered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Which observation this alert is about — we only re-alert when it changes.
    price_point_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("price_points.id", ondelete="SET NULL"), nullable=True
    )


class TrackAlertState(Base, UUIDPKMixin, TimestampMixin):
    """What we last told this user about a tracked product — prevents repeat alerts."""

    __tablename__ = "track_alert_states"
    __table_args__ = (UniqueConstraint("tracked_product_id", name="uq_track_alert_state"),)

    tracked_product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tracked_products.id", ondelete="CASCADE"), index=True, nullable=False
    )
    last_price_point_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("price_points.id", ondelete="SET NULL"), nullable=True
    )
    last_alert_kind: Mapped[str | None] = mapped_column(String(30), nullable=True)
    alerts_sent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
