"""Normalized affiliate campaigns kept separate from customer-facing coupons."""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin

_JSON = JSON().with_variant(JSONB(), "postgresql")


class AffiliateCampaign(Base, UUIDPKMixin, TimestampMixin):
    """A network campaign, not a coupon or a claim about a shopper discount."""

    __tablename__ = "affiliate_campaigns"
    __table_args__ = (
        UniqueConstraint(
            "network", "external_campaign_id", name="uq_affiliate_campaign_network_id"
        ),
        Index("ix_affiliate_campaigns_active_name", "is_active", "merchant_name"),
        Index("ix_affiliate_campaigns_store_active", "store_id", "is_active"),
    )

    network: Mapped[str] = mapped_column(String(32), nullable=False, default="vcommission")
    external_campaign_id: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    kpi: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    merchant_name: Mapped[str] = mapped_column(String(200), nullable=False)
    store_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("stores.id", ondelete="SET NULL"), nullable=True
    )
    preview_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    tracking_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    thumbnail_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    categories: Mapped[list[str]] = mapped_column(_JSON, nullable=False, default=list)
    countries: Mapped[list[str]] = mapped_column(_JSON, nullable=False, default=list)
    conversion_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    payouts: Mapped[object | None] = mapped_column(_JSON, nullable=True)
    default_goal: Mapped[object | None] = mapped_column(_JSON, nullable=True)
    goals: Mapped[object | None] = mapped_column(_JSON, nullable=True)
    device: Mapped[object | None] = mapped_column(_JSON, nullable=True)
    operating_system: Mapped[object | None] = mapped_column(_JSON, nullable=True)
    app_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    app_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    network_metadata: Mapped[dict] = mapped_column(_JSON, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AffiliateCampaignSync(Base, UUIDPKMixin):
    """One operational record per campaign-feed synchronization attempt."""

    __tablename__ = "affiliate_campaign_syncs"
    __table_args__ = (Index("ix_affiliate_campaign_syncs_started_at", "started_at"),)

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pages_fetched: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    unchanged: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    deactivated: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
