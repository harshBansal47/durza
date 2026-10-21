"""analytics content events and rollups

Revision ID: 34ae35ad3935
Revises: d06c8ddc0123
Create Date: 2026-10-21
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "34ae35ad3935"
down_revision = "d06c8ddc0123"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'analytics_viewer'")
    op.add_column("analytics_events", sa.Column("content_id", sa.String(length=80), nullable=True))
    op.add_column("analytics_events", sa.Column("content_type", sa.String(length=30), nullable=True))
    op.add_column("analytics_events", sa.Column("list_id", sa.String(length=80), nullable=True))
    op.add_column("analytics_events", sa.Column("position", sa.Integer(), nullable=True))
    op.add_column("analytics_events", sa.Column("ranking_version", sa.String(length=20), nullable=True))
    op.add_column("analytics_events", sa.Column("schema_version", sa.Integer(), nullable=True))

    op.add_column("coupon_impressions", sa.Column("list_id", sa.String(length=80), nullable=True))

    op.create_table(
        "daily_content_stats",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("content_ref", sa.String(length=80), nullable=False),
        sa.Column("content_type", sa.String(length=30), nullable=False),
        sa.Column("views", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scroll_25", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scroll_50", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scroll_75", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scroll_100", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("engaged_time_sum_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("engaged_time_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("outbound_clicks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("conversions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confirmed_revenue", sa.Numeric(12, 2), nullable=False, server_default="0.0"),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("date", "content_ref", "content_type", name="uq_daily_content_stats"),
    )
    op.create_index("ix_daily_content_stats_ref_date", "daily_content_stats", ["content_ref", "date"])


def downgrade() -> None:
    op.drop_index("ix_daily_content_stats_ref_date", table_name="daily_content_stats")
    op.drop_table("daily_content_stats")
    op.drop_column("coupon_impressions", "list_id")
    for col in ("schema_version", "ranking_version", "position", "list_id", "content_type", "content_id"):
        op.drop_column("analytics_events", col)
