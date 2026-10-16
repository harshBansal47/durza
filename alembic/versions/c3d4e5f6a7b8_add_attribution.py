"""add attribution table

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-10-04 18:30:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attributions",
        sa.Column("visitor_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("coupon_id", sa.Uuid(), nullable=True),
        sa.Column("merchant_id", sa.Uuid(), nullable=True),
        sa.Column("click_id", sa.String(length=64), nullable=True),
        sa.Column("click_event_id", sa.Uuid(), nullable=True),
        sa.Column("provider_id", sa.Uuid(), nullable=True),
        sa.Column("program_id", sa.Uuid(), nullable=True),
        sa.Column(
            "attribution_model",
            sa.Enum(
                "click_touch",
                "first_touch",
                "last_touch",
                "linear",
                name="attribution_model",
                native_enum=True,
            ),
            server_default="click_touch",
            nullable=False,
        ),
        sa.Column("first_touch_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_touch_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attribution_window", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("attribution_reason", sa.String(length=200), nullable=True),
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
        sa.ForeignKeyConstraint(["coupon_id"], ["coupons.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["merchant_id"], ["stores.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["click_event_id"], ["click_events.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["provider_id"], ["affiliate_providers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["program_id"], ["affiliate_programs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_attributions_click_id", "attributions", ["click_id"])
    op.create_index("ix_attributions_visitor", "attributions", ["visitor_id"])
    op.create_index("ix_attributions_session", "attributions", ["session_id"])
    op.create_index("ix_attributions_merchant", "attributions", ["merchant_id"])


def downgrade() -> None:
    op.drop_index("ix_attributions_merchant", table_name="attributions")
    op.drop_index("ix_attributions_session", table_name="attributions")
    op.drop_index("ix_attributions_visitor", table_name="attributions")
    op.drop_index("ix_attributions_click_id", table_name="attributions")
    op.drop_table("attributions")
    op.execute("DROP TYPE IF EXISTS attribution_model")
