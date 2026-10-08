import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin


class EditorialContent(Base, UUIDPKMixin, TimestampMixin):
    """Published buying advice and curated savings content."""

    __tablename__ = "editorial_contents"
    __table_args__ = (
        CheckConstraint(
            "content_type IN ('guide', 'review', 'comparison', 'collection')",
            name="ck_editorial_content_type",
        ),
        CheckConstraint(
            "status IN ('draft', 'in_review', 'published', 'archived')",
            name="ck_editorial_status",
        ),
    )

    content_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(20), default="draft", server_default="draft", nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    slug: Mapped[str] = mapped_column(String(260), unique=True, index=True, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_blocks: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    structured_data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    hero_image_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    author_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    market: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)
    seo_title: Mapped[str | None] = mapped_column(String(240), nullable=True)
    meta_description: Mapped[str | None] = mapped_column(String(300), nullable=True)
    canonical_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    is_indexable: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
    is_featured: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False, index=True
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EditorialRelation(Base, UUIDPKMixin, TimestampMixin):
    """Curated editorial-to-editorial links within a topical cluster."""

    __tablename__ = "editorial_relations"
    __table_args__ = (
        CheckConstraint("content_id != related_content_id", name="ck_editorial_not_related_to_self"),
        UniqueConstraint(
            "content_id", "related_content_id", "relation_type", name="uq_editorial_relation"
        ),
        CheckConstraint(
            "relation_type IN ('related', 'supports', 'alternative', 'collection_member')",
            name="ck_editorial_relation_type",
        ),
    )

    content_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("editorial_contents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    related_content_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("editorial_contents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relation_type: Mapped[str] = mapped_column(
        String(30), default="related", server_default="related", nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)


class EditorialEntityLink(Base, UUIDPKMixin, TimestampMixin):
    """A typed editorial link to an existing store, product, offer, or category."""

    __tablename__ = "editorial_entity_links"
    __table_args__ = (
        CheckConstraint(
            "(CASE WHEN store_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN product_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN coupon_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN category_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_editorial_entity_exactly_one_target",
        ),
        CheckConstraint(
            "link_type IN ('primary', 'related', 'mentioned', 'featured')",
            name="ck_editorial_entity_link_type",
        ),
    )

    content_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("editorial_contents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=True, index=True
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=True, index=True
    )
    coupon_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("coupons.id", ondelete="CASCADE"), nullable=True, index=True
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("categories.id", ondelete="CASCADE"), nullable=True, index=True
    )
    link_type: Mapped[str] = mapped_column(
        String(20), default="related", server_default="related", nullable=False
    )
    anchor_text: Mapped[str | None] = mapped_column(String(180), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)


class EditorialFAQ(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "editorial_faqs"

    content_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("editorial_contents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question: Mapped[str] = mapped_column(String(500), nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)


class StoreFAQ(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "store_faqs"

    store_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question: Mapped[str] = mapped_column(String(500), nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)


class StoreRelationship(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "store_relationships"
    __table_args__ = (
        UniqueConstraint("store_id", "related_store_id", name="uq_store_relationship"),
        CheckConstraint("store_id != related_store_id", name="ck_store_not_related_to_self"),
    )

    store_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    related_store_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relationship_type: Mapped[str] = mapped_column(
        String(30), default="alternative", server_default="alternative", nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
