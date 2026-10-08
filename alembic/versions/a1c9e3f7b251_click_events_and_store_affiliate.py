"""click events and store affiliate settings

Revision ID: a1c9e3f7b251
Revises: ef06a153c119
Create Date: 2026-10-03 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a1c9e3f7b251"
down_revision = "ef06a153c119"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stores",
        sa.Column("affiliate_network", sa.String(length=20), server_default="none", nullable=False),
    )
    op.add_column("stores", sa.Column("link_template", sa.Text(), nullable=True))
    op.add_column("stores", sa.Column("cookie_days", sa.Integer(), nullable=True))

    op.create_table(
        "click_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("clickref", sa.String(length=32), nullable=False),
        sa.Column("coupon_id", sa.Uuid(), nullable=True),
        sa.Column("product_id", sa.Uuid(), nullable=True),
        sa.Column("store_id", sa.Uuid(), nullable=True),
        sa.Column("src", sa.String(length=40), nullable=False),
        sa.Column("network", sa.String(length=20), nullable=False),
        sa.Column("visitor_hash", sa.String(length=64), nullable=True),
        sa.Column("is_bot", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["coupon_id"], ["coupons.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_click_events_clickref", "click_events", ["clickref"], unique=True)
    op.create_index("ix_click_events_coupon_id", "click_events", ["coupon_id"])
    op.create_index("ix_click_events_product_id", "click_events", ["product_id"])
    op.create_index("ix_click_events_store_created", "click_events", ["store_id", "created_at"])
    op.create_index("ix_click_events_dedupe", "click_events", ["visitor_hash", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_click_events_dedupe", table_name="click_events")
    op.drop_index("ix_click_events_store_created", table_name="click_events")
    op.drop_index("ix_click_events_product_id", table_name="click_events")
    op.drop_index("ix_click_events_coupon_id", table_name="click_events")
    op.drop_index("ix_click_events_clickref", table_name="click_events")
    op.drop_table("click_events")
    op.drop_column("stores", "cookie_days")
    op.drop_column("stores", "link_template")
    op.drop_column("stores", "affiliate_network")
