import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin


class CouponStaging(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "coupon_staging"

    __table_args__ = (
        UniqueConstraint(
            "source_id", "external_id", "content_hash", name="uq_coupon_staging_dedupe"
        ),
        Index("ix_coupon_staging_source", "source_id"),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    store_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("stores.id", ondelete="SET NULL"), nullable=True
    )
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="quarantined", server_default="quarantined", nullable=False
    )
    reason_code: Mapped[str] = mapped_column(String(60), nullable=False)
    observed_value: Mapped[str | None] = mapped_column(String(120), nullable=True)
    threshold_value: Mapped[str | None] = mapped_column(String(120), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
