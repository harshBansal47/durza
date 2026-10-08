"""add daily analytics stats tables

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-10-04 19:30:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- daily_coupon_stats ----
    op.create_table(
        "daily_coupon_stats",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("coupon_id", sa.Uuid(), nullable=False),
        sa.Column("impressions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("clicks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("conversions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revenue", sa.Numeric(precision=12, scale=2, asdecimal=False), nullable=False, server_default="0.0"),
        sa.Column("confirmed_revenue", sa.Numeric(precision=12, scale=2, asdecimal=False), nullable=False, server_default="0.0"),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("ctr", sa.Numeric(precision=10, scale=4, asdecimal=False), nullable=True),
        sa.Column("conversion_rate", sa.Numeric(precision=10, scale=4, asdecimal=False), nullable=True),
        sa.Column("epc", sa.Numeric(precision=10, scale=4, asdecimal=False), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["coupon_id"], ["coupons.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("date", "coupon_id", name="uq_daily_coupon_stats"),
    )
    op.create_index("ix_daily_coupon_stats_coupon_date", "daily_coupon_stats", ["coupon_id", "date"])

    # ---- daily_merchant_stats ----
    op.create_table(
        "daily_merchant_stats",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("impressions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("clicks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("conversions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revenue", sa.Numeric(precision=12, scale=2, asdecimal=False), nullable=False, server_default="0.0"),
        sa.Column("confirmed_revenue", sa.Numeric(precision=12, scale=2, asdecimal=False), nullable=False, server_default="0.0"),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("ctr", sa.Numeric(precision=10, scale=4, asdecimal=False), nullable=True),
        sa.Column("conversion_rate", sa.Numeric(precision=10, scale=4, asdecimal=False), nullable=True),
        sa.Column("epc", sa.Numeric(precision=10, scale=4, asdecimal=False), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["merchant_id"], ["stores.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("date", "merchant_id", name="uq_daily_merchant_stats"),
    )
    op.create_index("ix_daily_merchant_stats_merchant_date", "daily_merchant_stats", ["merchant_id", "date"])

    # ---- daily_provider_stats ----
    op.create_table(
        "daily_provider_stats",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("clicks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("conversions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("commission", sa.Numeric(precision=12, scale=2, asdecimal=False), nullable=False, server_default="0.0"),
        sa.Column("confirmed_commission", sa.Numeric(precision=12, scale=2, asdecimal=False), nullable=False, server_default="0.0"),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("conversion_rate", sa.Numeric(precision=10, scale=4, asdecimal=False), nullable=True),
        sa.Column("epc", sa.Numeric(precision=10, scale=4, asdecimal=False), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["provider_id"], ["affiliate_providers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("date", "provider_id", name="uq_daily_provider_stats"),
    )
    op.create_index("ix_daily_provider_stats_provider_date", "daily_provider_stats", ["provider_id", "date"])

    # ---- daily_traffic_stats ----
    op.create_table(
        "daily_traffic_stats",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("impressions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("clicks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("conversions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revenue", sa.Numeric(precision=12, scale=2, asdecimal=False), nullable=False, server_default="0.0"),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("ctr", sa.Numeric(precision=10, scale=4, asdecimal=False), nullable=True),
        sa.Column("conversion_rate", sa.Numeric(precision=10, scale=4, asdecimal=False), nullable=True),
        sa.Column("epc", sa.Numeric(precision=10, scale=4, asdecimal=False), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("date", "source", name="uq_daily_traffic_stats"),
    )
    op.create_index("ix_daily_traffic_stats_date", "daily_traffic_stats", ["date"])


def downgrade() -> None:
    op.drop_index("ix_daily_traffic_stats_date", table_name="daily_traffic_stats")
    op.drop_table("daily_traffic_stats")
    op.drop_index("ix_daily_provider_stats_provider_date", table_name="daily_provider_stats")
    op.drop_table("daily_provider_stats")
    op.drop_index("ix_daily_merchant_stats_merchant_date", table_name="daily_merchant_stats")
    op.drop_table("daily_merchant_stats")
    op.drop_index("ix_daily_coupon_stats_coupon_date", table_name="daily_coupon_stats")
    op.drop_table("daily_coupon_stats")
