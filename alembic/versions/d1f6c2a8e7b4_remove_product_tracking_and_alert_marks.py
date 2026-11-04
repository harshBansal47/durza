"""remove product tracking and per-user alert marks

Revision ID: d1f6c2a8e7b4
Revises: c9d2b7a104ef
Create Date: 2026-11-02

"""

import sqlalchemy as sa

from alembic import op

revision = "d1f6c2a8e7b4"
down_revision = "c9d2b7a104ef"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("deal_alert_marks")
    op.drop_table("track_alert_states")
    op.drop_table("tracked_products")


def downgrade() -> None:
    op.create_table(
        "tracked_products",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("target_price", sa.Float(), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "product_id", name="uq_tracked_product"),
    )
    op.create_index("ix_tracked_products_user_id", "tracked_products", ["user_id"], unique=False)
    op.create_index(
        "ix_tracked_products_product_id", "tracked_products", ["product_id"], unique=False
    )
    op.create_table(
        "track_alert_states",
        sa.Column("tracked_product_id", sa.Uuid(), nullable=False),
        sa.Column("last_notified_price_point_id", sa.Uuid(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["tracked_product_id"], ["tracked_products.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["last_notified_price_point_id"], ["price_points.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tracked_product_id", name="uq_track_alert_state"),
    )
    op.create_index(
        "ix_track_alert_states_tracked_product_id",
        "track_alert_states",
        ["tracked_product_id"],
        unique=False,
    )
    op.create_table(
        "deal_alert_marks",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("coupon_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["coupon_id"], ["coupons.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "coupon_id", "kind", name="uq_deal_alert_marks_user_coupon_kind"
        ),
    )
    op.create_index("ix_deal_alert_marks_user_id", "deal_alert_marks", ["user_id"], unique=False)
    op.create_index(
        "ix_deal_alert_marks_coupon_id", "deal_alert_marks", ["coupon_id"], unique=False
    )
