import enum
import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
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
    # Category IDs the user has muted — alerts for products in these categories
    # are skipped. Stored as a JSON list of UUID strings for easy PATCH updates.
    muted_categories: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)


class AlertKind(str, enum.Enum):
    price_drop = "price_drop"
    coupon_appeared = "coupon_appeared"
    target_met = "target_met"  # effective price fell below the user's target
    # Deal alerts (`deal_alert_service`): about stores a user follows and offers
    # they track, rather than a product's price.
    new_offer = "new_offer"  # a followed store has a new live offer
    confirmed = "confirmed"  # a tracked offer was confirmed working
    ending_soon = "ending_soon"  # a tracked offer expires within the warning window
    ended = "ended"  # a tracked offer expired, or stopped working


class AlertEvent(Base, UUIDPKMixin, TimestampMixin):
    """Every alert we actually sent (or attempted) — history + dedupe guard."""

    __tablename__ = "alert_events"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Null for deal alerts, which are about a store or an offer, not a product.
    tracked_product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tracked_products.id", ondelete="CASCADE"), index=True, nullable=True
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
    # Deal alerts: the offer / store the alert leads with (a digest may mention
    # more), and one id shared by every channel row of the same alert, so the
    # history can show one alert that went out on three channels as one alert.
    coupon_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("coupons.id", ondelete="SET NULL"), nullable=True
    )
    store_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("stores.id", ondelete="SET NULL"), nullable=True
    )
    batch_id: Mapped[uuid.UUID | None] = mapped_column(index=True, nullable=True)


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


class DealAlertMark(Base, UUIDPKMixin, TimestampMixin):
    """ "We already told this user about this" — one row per (user, offer, kind).

    Deal alerts are derived from the current state of offers rather than from a
    stream of events, so without a memory every scan would re-send everything.
    A mark is written only after a channel actually accepted the send, which
    means a failed delivery is retried on the next scan instead of being lost.
    """

    __tablename__ = "deal_alert_marks"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "coupon_id", "kind", name="uq_deal_alert_marks_user_coupon_kind"
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    coupon_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("coupons.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
