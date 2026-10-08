"""smart_savings_editorial

Revision ID: f3a41b7c9d20
Revises: c9e1a4d27b50
Create Date: 2026-10-08

"""

import sqlalchemy as sa

from alembic import op

revision = "f3a41b7c9d20"
down_revision = "c9e1a4d27b50"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("stores", sa.Column("pricing_summary", sa.JSON(), nullable=True))
    op.add_column("stores", sa.Column("pricing_checked_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "editorial_contents",
        sa.Column("content_type", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="draft", nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("slug", sa.String(length=260), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("content_blocks", sa.JSON(), nullable=False),
        sa.Column("structured_data", sa.JSON(), nullable=False),
        sa.Column("hero_image_url", sa.String(length=1000), nullable=True),
        sa.Column("author_name", sa.String(length=160), nullable=True),
        sa.Column("market", sa.String(length=2), nullable=True),
        sa.Column("seo_title", sa.String(length=240), nullable=True),
        sa.Column("meta_description", sa.String(length=300), nullable=True),
        sa.Column("canonical_url", sa.String(length=1000), nullable=True),
        sa.Column("is_indexable", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_featured", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "content_type IN ('guide', 'review', 'comparison', 'collection')",
            name="ck_editorial_content_type",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'in_review', 'published', 'archived')",
            name="ck_editorial_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_editorial_contents_content_type", "editorial_contents", ["content_type"])
    op.create_index("ix_editorial_contents_status", "editorial_contents", ["status"])
    op.create_index("ix_editorial_contents_slug", "editorial_contents", ["slug"], unique=True)
    op.create_index("ix_editorial_contents_market", "editorial_contents", ["market"])
    op.create_index("ix_editorial_contents_is_featured", "editorial_contents", ["is_featured"])

    op.create_table(
        "editorial_relations",
        sa.Column("content_id", sa.Uuid(), nullable=False),
        sa.Column("related_content_id", sa.Uuid(), nullable=False),
        sa.Column("relation_type", sa.String(length=30), server_default="related", nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "relation_type IN ('related', 'supports', 'alternative', 'collection_member')",
            name="ck_editorial_relation_type",
        ),
        sa.CheckConstraint(
            "content_id != related_content_id", name="ck_editorial_not_related_to_self"
        ),
        sa.ForeignKeyConstraint(["content_id"], ["editorial_contents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["related_content_id"], ["editorial_contents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "content_id", "related_content_id", "relation_type", name="uq_editorial_relation"
        ),
    )
    op.create_index("ix_editorial_relations_content_id", "editorial_relations", ["content_id"])
    op.create_index(
        "ix_editorial_relations_related_content_id", "editorial_relations", ["related_content_id"]
    )

    op.create_table(
        "editorial_entity_links",
        sa.Column("content_id", sa.Uuid(), nullable=False),
        sa.Column("store_id", sa.Uuid(), nullable=True),
        sa.Column("product_id", sa.Uuid(), nullable=True),
        sa.Column("coupon_id", sa.Uuid(), nullable=True),
        sa.Column("category_id", sa.Uuid(), nullable=True),
        sa.Column("link_type", sa.String(length=20), server_default="related", nullable=False),
        sa.Column("anchor_text", sa.String(length=180), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "(CASE WHEN store_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN product_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN coupon_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN category_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_editorial_entity_exactly_one_target",
        ),
        sa.CheckConstraint(
            "link_type IN ('primary', 'related', 'mentioned', 'featured')",
            name="ck_editorial_entity_link_type",
        ),
        sa.ForeignKeyConstraint(["content_id"], ["editorial_contents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["coupon_id"], ["coupons.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("content_id", "store_id", "product_id", "coupon_id", "category_id"):
        op.create_index(f"ix_editorial_entity_links_{column}", "editorial_entity_links", [column])

    op.create_table(
        "editorial_faqs",
        sa.Column("content_id", sa.Uuid(), nullable=False),
        sa.Column("question", sa.String(length=500), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["content_id"], ["editorial_contents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_editorial_faqs_content_id", "editorial_faqs", ["content_id"])

    op.create_table(
        "store_faqs",
        sa.Column("store_id", sa.Uuid(), nullable=False),
        sa.Column("question", sa.String(length=500), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_store_faqs_store_id", "store_faqs", ["store_id"])

    op.create_table(
        "store_relationships",
        sa.Column("store_id", sa.Uuid(), nullable=False),
        sa.Column("related_store_id", sa.Uuid(), nullable=False),
        sa.Column("relationship_type", sa.String(length=30), server_default="alternative", nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("store_id != related_store_id", name="ck_store_not_related_to_self"),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["related_store_id"], ["stores.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("store_id", "related_store_id", name="uq_store_relationship"),
    )
    op.create_index("ix_store_relationships_store_id", "store_relationships", ["store_id"])
    op.create_index(
        "ix_store_relationships_related_store_id", "store_relationships", ["related_store_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_store_relationships_related_store_id", table_name="store_relationships")
    op.drop_index("ix_store_relationships_store_id", table_name="store_relationships")
    op.drop_table("store_relationships")
    op.drop_index("ix_store_faqs_store_id", table_name="store_faqs")
    op.drop_table("store_faqs")
    op.drop_index("ix_editorial_faqs_content_id", table_name="editorial_faqs")
    op.drop_table("editorial_faqs")
    for column in ("content_id", "store_id", "product_id", "coupon_id", "category_id"):
        op.drop_index(f"ix_editorial_entity_links_{column}", table_name="editorial_entity_links")
    op.drop_table("editorial_entity_links")
    op.drop_index(
        "ix_editorial_relations_related_content_id", table_name="editorial_relations"
    )
    op.drop_index("ix_editorial_relations_content_id", table_name="editorial_relations")
    op.drop_table("editorial_relations")
    op.drop_index("ix_editorial_contents_is_featured", table_name="editorial_contents")
    op.drop_index("ix_editorial_contents_market", table_name="editorial_contents")
    op.drop_index("ix_editorial_contents_slug", table_name="editorial_contents")
    op.drop_index("ix_editorial_contents_status", table_name="editorial_contents")
    op.drop_index("ix_editorial_contents_content_type", table_name="editorial_contents")
    op.drop_table("editorial_contents")
    op.drop_column("stores", "pricing_checked_at")
    op.drop_column("stores", "pricing_summary")
