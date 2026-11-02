"""remove retained per-user alert history

Revision ID: c9d2b7a104ef
Revises: 8f4d1c2b6a90
Create Date: 2026-11-02

"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c9d2b7a104ef"
down_revision = "8f4d1c2b6a90"
branch_labels = None
depends_on = None


_ALERT_KINDS = (
    "price_drop",
    "coupon_appeared",
    "target_met",
    "new_offer",
    "confirmed",
    "ending_soon",
    "ended",
)


def upgrade() -> None:
    op.drop_table("alert_events")

    with op.batch_alter_table("track_alert_states") as batch:
        batch.drop_column("last_alert_kind")
        batch.drop_column("alerts_sent")
        batch.alter_column(
            "last_price_point_id",
            new_column_name="last_notified_price_point_id",
            existing_type=sa.Uuid(),
            existing_nullable=True,
        )

    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS alert_kind")


def downgrade() -> None:
    alert_kind = postgresql.ENUM(*_ALERT_KINDS, name="alert_kind", create_type=False)
    if op.get_bind().dialect.name == "postgresql":
        alert_kind.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "alert_events",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("tracked_product_id", sa.Uuid(), nullable=True),
        sa.Column("kind", alert_kind, nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("delivered", sa.Boolean(), nullable=False),
        sa.Column("price_point_id", sa.Uuid(), nullable=True),
        sa.Column("coupon_id", sa.Uuid(), nullable=True),
        sa.Column("store_id", sa.Uuid(), nullable=True),
        sa.Column("batch_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tracked_product_id"], ["tracked_products.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["price_point_id"], ["price_points.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["coupon_id"], ["coupons.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_alert_events_user_id"), "alert_events", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_alert_events_tracked_product_id"),
        "alert_events",
        ["tracked_product_id"],
        unique=False,
    )
    op.create_index(op.f("ix_alert_events_batch_id"), "alert_events", ["batch_id"], unique=False)

    with op.batch_alter_table("track_alert_states") as batch:
        batch.alter_column(
            "last_notified_price_point_id",
            new_column_name="last_price_point_id",
            existing_type=sa.Uuid(),
            existing_nullable=True,
        )
        batch.add_column(sa.Column("last_alert_kind", sa.String(length=30), nullable=True))
        batch.add_column(
            sa.Column("alerts_sent", sa.Integer(), nullable=False, server_default="0")
        )
        batch.alter_column("alerts_sent", server_default=None)
