"""persist normalized vCommission affiliate campaigns

Revision ID: b6c1d9e2f304
Revises: a7d3c9e5b1f2
Create Date: 2026-11-05

"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b6c1d9e2f304"
down_revision = "a7d3c9e5b1f2"
branch_labels = None
depends_on = None


def _json_type():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "affiliate_campaigns",
        sa.Column("network", sa.String(length=32), nullable=False),
        sa.Column("external_campaign_id", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("kpi", sa.String(length=2000), nullable=True),
        sa.Column("merchant_name", sa.String(length=200), nullable=False),
        sa.Column("store_id", sa.Uuid(), nullable=True),
        sa.Column("preview_url", sa.String(length=2048), nullable=True),
        sa.Column("tracking_url", sa.String(length=2048), nullable=False),
        sa.Column("thumbnail_url", sa.String(length=2048), nullable=True),
        sa.Column("categories", _json_type(), nullable=False),
        sa.Column("countries", _json_type(), nullable=False),
        sa.Column("conversion_model", sa.String(length=120), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("payouts", _json_type(), nullable=True),
        sa.Column("default_goal", _json_type(), nullable=True),
        sa.Column("goals", _json_type(), nullable=True),
        sa.Column("device", _json_type(), nullable=True),
        sa.Column("operating_system", _json_type(), nullable=True),
        sa.Column("app_name", sa.String(length=255), nullable=True),
        sa.Column("app_id", sa.String(length=255), nullable=True),
        sa.Column("network_metadata", _json_type(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "id",
            sa.Uuid(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "network", "external_campaign_id", name="uq_affiliate_campaign_network_id"
        ),
    )
    op.create_index(
        "ix_affiliate_campaigns_active_name",
        "affiliate_campaigns",
        ["is_active", "merchant_name"],
    )
    op.create_index(
        "ix_affiliate_campaigns_store_active",
        "affiliate_campaigns",
        ["store_id", "is_active"],
    )

    op.create_table(
        "affiliate_campaign_syncs",
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pages_fetched", sa.Integer(), server_default="0", nullable=False),
        sa.Column("inserted", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated", sa.Integer(), server_default="0", nullable=False),
        sa.Column("unchanged", sa.Integer(), server_default="0", nullable=False),
        sa.Column("skipped", sa.Integer(), server_default="0", nullable=False),
        sa.Column("deactivated", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_affiliate_campaign_syncs_started_at",
        "affiliate_campaign_syncs",
        ["started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_affiliate_campaign_syncs_started_at", table_name="affiliate_campaign_syncs")
    op.drop_table("affiliate_campaign_syncs")
    op.drop_index("ix_affiliate_campaigns_store_active", table_name="affiliate_campaigns")
    op.drop_index("ix_affiliate_campaigns_active_name", table_name="affiliate_campaigns")
    op.drop_table("affiliate_campaigns")
