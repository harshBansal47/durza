"""add analytics tracking tables

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-10-04 18:00:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- analytics_sessions ----
    op.create_table(
        "analytics_sessions",
        sa.Column("visitor_id", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("landing_page", sa.String(length=500), nullable=True),
        sa.Column("landing_path", sa.String(length=500), nullable=True),
        sa.Column("referrer", sa.Text(), nullable=True),
        sa.Column("referrer_domain", sa.String(length=255), nullable=True),
        sa.Column("utm_source", sa.String(length=100), nullable=True),
        sa.Column("utm_medium", sa.String(length=100), nullable=True),
        sa.Column("utm_campaign", sa.String(length=200), nullable=True),
        sa.Column("utm_content", sa.String(length=200), nullable=True),
        sa.Column("utm_term", sa.String(length=200), nullable=True),
        sa.Column(
            "device_type",
            sa.Enum("desktop", "mobile", "tablet", "other", name="device_type", native_enum=True),
            server_default="other",
            nullable=False,
        ),
        sa.Column("browser", sa.String(length=100), nullable=True),
        sa.Column("country", sa.String(length=2), nullable=True),
        sa.Column(
            "traffic_source",
            sa.Enum(
                "seo",
                "direct",
                "social",
                "email",
                "referral",
                "paid",
                "other",
                name="traffic_source",
                native_enum=True,
            ),
            server_default="direct",
            nullable=False,
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_analytics_sessions_visitor", "analytics_sessions", ["visitor_id"])
    op.create_index("ix_analytics_sessions_started", "analytics_sessions", ["started_at"])
    op.create_index("ix_analytics_sessions_source", "analytics_sessions", ["traffic_source"])

    # ---- coupon_impressions ----
    op.create_table(
        "coupon_impressions",
        sa.Column("coupon_id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("visitor_id", sa.String(length=64), nullable=True),
        sa.Column(
            "page_type",
            sa.Enum(
                "coupon_card",
                "coupon_detail",
                "merchant_page",
                "category_page",
                "search_results",
                "homepage",
                "featured_section",
                "sponsored_section",
                name="impression_type",
                native_enum=True,
            ),
            nullable=False,
        ),
        sa.Column("page_id", sa.String(length=255), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("ranking_version", sa.String(length=20), server_default="v1", nullable=False),
        sa.Column("ranking_score", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=100), nullable=True),
        sa.Column("referrer", sa.String(length=500), nullable=True),
        sa.Column("device_type", sa.String(length=20), nullable=True),
        sa.Column("country", sa.String(length=2), nullable=True),
        sa.Column("sponsored", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["coupon_id"], ["coupons.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["merchant_id"], ["stores.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_coupon_impressions_coupon_created", "coupon_impressions", ["coupon_id", "created_at"]
    )
    op.create_index(
        "ix_coupon_impressions_page_type_created", "coupon_impressions", ["page_type", "created_at"]
    )
    op.create_index(
        "ix_coupon_impressions_merchant_created",
        "coupon_impressions",
        ["merchant_id", "created_at"],
    )
    op.create_index("ix_coupon_impressions_source", "coupon_impressions", ["source"])


def downgrade() -> None:
    # All tables before all types: Postgres refuses to drop a type a live column
    # still depends on, so interleaving only works while no two tables share a
    # type — an invisible property of the schema. See the same note in
    # a1b2c3d4e5f6_add_affiliate_monetization.
    op.drop_index("ix_coupon_impressions_source", table_name="coupon_impressions")
    op.drop_index("ix_coupon_impressions_merchant_created", table_name="coupon_impressions")
    op.drop_index("ix_coupon_impressions_page_type_created", table_name="coupon_impressions")
    op.drop_index("ix_coupon_impressions_coupon_created", table_name="coupon_impressions")
    op.drop_table("coupon_impressions")

    op.drop_index("ix_analytics_sessions_source", table_name="analytics_sessions")
    op.drop_index("ix_analytics_sessions_started", table_name="analytics_sessions")
    op.drop_index("ix_analytics_sessions_visitor", table_name="analytics_sessions")
    op.drop_table("analytics_sessions")

    op.execute("DROP TYPE IF EXISTS impression_type")
    op.execute("DROP TYPE IF EXISTS traffic_source")
    op.execute("DROP TYPE IF EXISTS device_type")
