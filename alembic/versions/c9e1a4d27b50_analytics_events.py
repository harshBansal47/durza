"""analytics events: non-impression shopper interactions

Revision ID: c9e1a4d27b50
Revises: b3c4d5e6f7a8
Create Date: 2026-10-06 12:00:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "c9e1a4d27b50"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analytics_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("visitor_id", sa.String(length=64), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("coupon_id", sa.Uuid(), nullable=True),
        sa.Column("store_id", sa.Uuid(), nullable=True),
        sa.Column("page_type", sa.String(length=30), nullable=True),
        sa.Column("props", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_analytics_events_coupon_id", "analytics_events", ["coupon_id"])
    op.create_index("ix_analytics_events_name_created", "analytics_events", ["name", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_analytics_events_name_created", table_name="analytics_events")
    op.drop_index("ix_analytics_events_coupon_id", table_name="analytics_events")
    op.drop_table("analytics_events")
