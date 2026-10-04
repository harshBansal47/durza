import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import UUIDPKMixin


class ClickEvent(Base, UUIDPKMixin):
    """One outbound click through a /go redirect.

    This is the join key between "someone left our site" and "an affiliate
    network later reported a sale". `clickref` is sent to the network on the
    outbound link and comes back on the transaction report, which is how
    earnings get attributed to a specific coupon, store and source page.

    Rows deliberately outlive the people in them: coupon/store/product foreign
    keys are SET NULL so deleting a coupon never erases revenue history, and
    `visitor_hash` is scrubbed after the retention window while the row stays.
    """

    __tablename__ = "click_events"
    __table_args__ = (
        Index("ix_click_events_store_created", "store_id", "created_at"),
        Index("ix_click_events_dedupe", "visitor_hash", "created_at"),
    )

    clickref: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    coupon_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("coupons.id", ondelete="SET NULL"), index=True, nullable=True
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("products.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    store_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("stores.id", ondelete="SET NULL"), nullable=True
    )
    # Set for clicks on sponsored placements. These earn no affiliate commission,
    # so revenue reports must not mix them into per-store earnings.
    ad_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("ads.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # Which surface sent the click ("coupon-page", "email", "telegram", ...).
    src: Mapped[str] = mapped_column(String(40), default="unknown", nullable=False)
    # Network the outbound link was built for, frozen at click time so a later
    # change to the store's settings does not rewrite history.
    network: Mapped[str] = mapped_column(String(20), default="none", nullable=False)
    # One-way hash, never a raw IP. Nulled by the retention job.
    visitor_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
