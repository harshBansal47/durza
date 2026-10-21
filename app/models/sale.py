import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin


class SaleSeries(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "sale_series"

    __table_args__ = (
        UniqueConstraint("slug", name="uq_sale_series_slug"),
        CheckConstraint("length(slug) <= 40", name="ck_sale_series_slug_length"),
    )

    slug: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    publication_status: Mapped[str] = mapped_column(
        String(20), default="draft", server_default="draft", nullable=False, index=True
    )
    __table_args__ = (
        UniqueConstraint("slug", name="uq_sale_series_slug"),
        CheckConstraint("length(slug) <= 40", name="ck_sale_series_slug_length"),
        CheckConstraint(
            "publication_status IN ('draft','in_review','published')",
            name="ck_sale_series_publication_status",
        ),
    )

    editions: Mapped[list["SaleEdition"]] = relationship(back_populates="series", cascade="all, delete-orphan")


class SaleEdition(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "sale_editions"

    __table_args__ = (
        UniqueConstraint("series_id", "slug", name="uq_sale_edition_series_slug"),
        CheckConstraint("length(slug) <= 30", name="ck_sale_edition_slug_length"),
        CheckConstraint(
            "publication_status IN ('draft','in_review','published')",
            name="ck_sale_edition_publication_status",
        ),
        CheckConstraint(
            "lifecycle_status IN ('announced','upcoming','live','ended','archived')",
            name="ck_sale_edition_lifecycle_status",
        ),
        Index("ix_sale_edition_series_slug", "series_id", "slug"),
        Index("ix_sale_edition_lifecycle", "lifecycle_status"),
        Index("ix_sale_edition_start", "start_at"),
        Index("ix_sale_edition_end", "end_at"),
    )

    series_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sale_series.id", ondelete="CASCADE"), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(30), nullable=False)
    year: Mapped[int] = mapped_column(nullable=False)
    publication_status: Mapped[str] = mapped_column(String(20), default="draft", server_default="draft", nullable=False, index=True)
    lifecycle_status: Mapped[str] = mapped_column(String(20), default="announced", server_default="announced", nullable=False, index=True)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dates_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    editorial_content_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("editorial_contents.id", ondelete="SET NULL"), nullable=True, index=True)
    coupon_rules_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    manual_override: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    manual_override_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    manual_override_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    manual_override_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_after_days: Mapped[int] = mapped_column(Integer, default=30, server_default="30", nullable=False)

    series: Mapped["SaleSeries"] = relationship(back_populates="editions")
    rules: Mapped[list["SaleEditionCouponRule"]] = relationship(back_populates="edition", cascade="all, delete-orphan")


class SaleEditionCouponRule(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "sale_edition_coupon_rules"

    __table_args__ = (
        CheckConstraint("rule_type IN ('store','tag','category')", name="ck_sale_rule_type"),
        Index("ix_sale_rules_edition", "edition_id"),
    )

    edition_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sale_editions.id", ondelete="CASCADE"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    rule_type: Mapped[str] = mapped_column(String(20), nullable=False)
    rule_value: Mapped[str] = mapped_column(String(160), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    edition: Mapped["SaleEdition"] = relationship(back_populates="rules")


class SaleSlugHistory(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "sale_slug_history"

    __table_args__ = (Index("ix_sale_slug_history_old", "old_slug"),)

    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    old_slug: Mapped[str] = mapped_column(String(80), nullable=False)
    new_slug: Mapped[str] = mapped_column(String(80), nullable=False)
