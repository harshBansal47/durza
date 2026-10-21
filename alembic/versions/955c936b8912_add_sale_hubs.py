"""add sale hubs

Revision ID: 955c936b8912
Revises: 34ae35ad3935
Create Date: 2026-10-21
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "955c936b8912"
down_revision = "34ae35ad3935"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sale_series",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("slug", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("publication_status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_sale_series_slug"),
        sa.CheckConstraint("length(slug) <= 40", name="ck_sale_series_slug_length"),
        sa.CheckConstraint("publication_status IN ('draft','in_review','published')", name="ck_sale_series_publication_status"),
    )
    op.create_index("ix_sale_series_slug", "sale_series", ["slug"])

    op.create_table(
        "sale_editions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("series_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(length=30), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("publication_status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("lifecycle_status", sa.String(length=20), nullable=False, server_default="announced"),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dates_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source_url", sa.String(length=1000), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("editorial_content_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("coupon_rules_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("manual_override", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("manual_override_reason", sa.String(length=500), nullable=True),
        sa.Column("manual_override_by", sa.String(length=160), nullable=True),
        sa.Column("manual_override_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_after_days", sa.Integer(), nullable=False, server_default="30"),
        sa.ForeignKeyConstraint(["series_id"], ["sale_series.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["editorial_content_id"], ["editorial_contents.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("series_id", "slug", name="uq_sale_edition_series_slug"),
        sa.CheckConstraint("length(slug) <= 30", name="ck_sale_edition_slug_length"),
        sa.CheckConstraint("publication_status IN ('draft','in_review','published')", name="ck_sale_edition_publication_status"),
        sa.CheckConstraint("lifecycle_status IN ('announced','upcoming','live','ended','archived')", name="ck_sale_edition_lifecycle_status"),
    )
    op.create_index("ix_sale_edition_series_slug", "sale_editions", ["series_id", "slug"])
    op.create_index("ix_sale_edition_lifecycle", "sale_editions", ["lifecycle_status"])
    op.create_index("ix_sale_edition_start", "sale_editions", ["start_at"])
    op.create_index("ix_sale_edition_end", "sale_editions", ["end_at"])

    op.create_table(
        "sale_edition_coupon_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("edition_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("rule_type", sa.String(length=20), nullable=False),
        sa.Column("rule_value", sa.String(length=160), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["edition_id"], ["sale_editions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("rule_type IN ('store','tag','category')", name="ck_sale_rule_type"),
    )
    op.create_index("ix_sale_rules_edition", "sale_edition_coupon_rules", ["edition_id"])

    op.create_table(
        "sale_slug_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entity_type", sa.String(length=20), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("old_slug", sa.String(length=80), nullable=False),
        sa.Column("new_slug", sa.String(length=80), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sale_slug_history_old", "sale_slug_history", ["old_slug"])
    op.create_index("ix_sale_slug_history_entity", "sale_slug_history", ["entity_id"])


def downgrade() -> None:
    op.drop_index("ix_sale_slug_history_entity", table_name="sale_slug_history")
    op.drop_index("ix_sale_slug_history_old", table_name="sale_slug_history")
    op.drop_table("sale_slug_history")
    op.drop_index("ix_sale_rules_edition", table_name="sale_edition_coupon_rules")
    op.drop_table("sale_edition_coupon_rules")
    op.drop_index("ix_sale_edition_end", table_name="sale_editions")
    op.drop_index("ix_sale_edition_start", table_name="sale_editions")
    op.drop_index("ix_sale_edition_lifecycle", table_name="sale_editions")
    op.drop_index("ix_sale_edition_series_slug", table_name="sale_editions")
    op.drop_table("sale_editions")
    op.drop_index("ix_sale_series_slug", table_name="sale_series")
    op.drop_table("sale_series")
