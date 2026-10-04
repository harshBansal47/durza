"""add revenue tables

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-10-04 19:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- affiliate_revenues ----
    op.create_table(
        "affiliate_revenues",
        sa.Column("conversion_id", sa.Uuid(), nullable=True),
        sa.Column("merchant_id", sa.Uuid(), nullable=True),
        sa.Column("provider_id", sa.Uuid(), nullable=True),
        sa.Column("program_id", sa.Uuid(), nullable=True),
        sa.Column("gross_commission", sa.Numeric(precision=12, scale=2, asdecimal=False), nullable=False, server_default="0.0"),
        sa.Column("adjustments", sa.Numeric(precision=12, scale=2, asdecimal=False), nullable=False, server_default="0.0"),
        sa.Column("net_commission", sa.Numeric(precision=12, scale=2, asdecimal=False), nullable=False, server_default="0.0"),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column(
            "status",
            sa.Enum("pending", "confirmed", "reversed", name="revenue_status", native_enum=True),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("conversion_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reported_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("normalized_amount", sa.Numeric(precision=12, scale=2, asdecimal=False), nullable=True),
        sa.Column("normalized_currency", sa.String(length=3), nullable=True),
        sa.Column("exchange_rate", sa.Numeric(precision=12, scale=6, asdecimal=False), nullable=True),
        sa.Column("exchange_rate_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["conversion_id"], ["conversions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["merchant_id"], ["stores.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["provider_id"], ["affiliate_providers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["program_id"], ["affiliate_programs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_affiliate_revenues_merchant_created", "affiliate_revenues", ["merchant_id", "created_at"])
    op.create_index("ix_affiliate_revenues_provider_created", "affiliate_revenues", ["provider_id", "created_at"])
    op.create_index("ix_affiliate_revenues_program_created", "affiliate_revenues", ["program_id", "created_at"])
    op.create_index("ix_affiliate_revenues_status", "affiliate_revenues", ["status"])

    # ---- revenue_events ----
    op.create_table(
        "revenue_events",
        sa.Column(
            "source_type",
            sa.Enum(
                "affiliate", "sponsored_placement", "merchant_subscription",
                "direct_campaign", "other",
                name="revenue_source_type", native_enum=True,
            ),
            nullable=False,
        ),
        sa.Column("source_id", sa.String(length=64), nullable=True),
        sa.Column("merchant_id", sa.Uuid(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=12, scale=2, asdecimal=False), nullable=False, server_default="0.0"),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("event_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("normalized_amount", sa.Numeric(precision=12, scale=2, asdecimal=False), nullable=True),
        sa.Column("normalized_currency", sa.String(length=3), nullable=True),
        sa.Column("exchange_rate", sa.Numeric(precision=12, scale=6, asdecimal=False), nullable=True),
        sa.Column("exchange_rate_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["merchant_id"], ["stores.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_revenue_events_merchant_created", "revenue_events", ["merchant_id", "created_at"])
    op.create_index("ix_revenue_events_source_type", "revenue_events", ["source_type"])
    op.create_index("ix_revenue_events_status", "revenue_events", ["status"])


def downgrade() -> None:
    # All tables before all types: Postgres refuses to drop a type a live column
    # still depends on, so interleaving only works while no two tables share a
    # type — an invisible property of the schema. See the same note in
    # a1b2c3d4e5f6_add_affiliate_monetization.
    op.drop_index("ix_revenue_events_status", table_name="revenue_events")
    op.drop_index("ix_revenue_events_source_type", table_name="revenue_events")
    op.drop_index("ix_revenue_events_merchant_created", table_name="revenue_events")
    op.drop_table("revenue_events")

    op.drop_index("ix_affiliate_revenues_status", table_name="affiliate_revenues")
    op.drop_index("ix_affiliate_revenues_program_created", table_name="affiliate_revenues")
    op.drop_index("ix_affiliate_revenues_provider_created", table_name="affiliate_revenues")
    op.drop_index("ix_affiliate_revenues_merchant_created", table_name="affiliate_revenues")
    op.drop_table("affiliate_revenues")

    op.execute("DROP TYPE IF EXISTS revenue_source_type")
    op.execute("DROP TYPE IF EXISTS revenue_status")
