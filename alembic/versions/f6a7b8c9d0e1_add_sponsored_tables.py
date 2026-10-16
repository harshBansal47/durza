"""add sponsored campaign tables

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-10-04 20:00:00.000000

"""

import sqlalchemy as sa

from alembic import op

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sponsored_campaigns",
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "status",
            sa.Enum("draft", "active", "paused", "ended", name="campaign_status", native_enum=True),
            server_default="draft",
            nullable=False,
        ),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("budget", sa.Numeric(precision=12, scale=2, asdecimal=False), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column(
            "campaign_model",
            sa.Enum(
                "cpm",
                "cpc",
                "cpa",
                "fixed_placement",
                "fixed_monthly",
                name="campaign_model",
                native_enum=True,
            ),
            server_default="fixed_placement",
            nullable=False,
        ),
        sa.Column("targeting_rules", sa.JSON(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["merchant_id"], ["stores.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "sponsored_placements",
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "placement_type",
            sa.Enum(
                "homepage",
                "merchant_page",
                "category_page",
                "search",
                "coupon_list",
                "featured_section",
                name="placement_type",
                native_enum=True,
            ),
            nullable=False,
        ),
        sa.Column("page_type", sa.String(length=50), nullable=False),
        sa.Column("position_rule", sa.String(length=50), nullable=False, server_default="fixed"),
        sa.Column("targeting", sa.JSON(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "max_impressions_per_visitor_per_day", sa.Integer(), nullable=False, server_default="3"
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["campaign_id"], ["sponsored_campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["merchant_id"], ["stores.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "sponsored_offers",
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("coupon_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["campaign_id"], ["sponsored_campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["merchant_id"], ["stores.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["coupon_id"], ["coupons.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("sponsored_offers")
    op.drop_table("sponsored_placements")
    op.drop_table("sponsored_campaigns")
    op.execute("DROP TYPE IF EXISTS campaign_status")
    op.execute("DROP TYPE IF EXISTS campaign_model")
    op.execute("DROP TYPE IF EXISTS placement_type")
