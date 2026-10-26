"""deal alerts: alerts about stores and offers, not just product prices

Revision ID: b7c3e5a1d9f2
Revises: a4f1c8e2d9b7
Create Date: 2026-10-26 09:00:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "b7c3e5a1d9f2"
down_revision = "a4f1c8e2d9b7"
branch_labels = None
depends_on = None

_NEW_KINDS = ("new_offer", "confirmed", "ending_soon", "ended")


def upgrade() -> None:
    # Postgres only lets a new enum value be used after the transaction that
    # added it commits (and older versions refuse ADD VALUE inside one at all),
    # so do this outside the migration's transaction.
    with op.get_context().autocommit_block():
        for kind in _NEW_KINDS:
            op.execute(f"ALTER TYPE alert_kind ADD VALUE IF NOT EXISTS '{kind}'")

    # A deal alert is about a store or an offer, not a tracked product.
    op.alter_column("alert_events", "tracked_product_id", existing_type=sa.Uuid(), nullable=True)
    op.add_column("alert_events", sa.Column("coupon_id", sa.Uuid(), nullable=True))
    op.add_column("alert_events", sa.Column("store_id", sa.Uuid(), nullable=True))
    op.add_column("alert_events", sa.Column("batch_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_alert_events_coupon_id",
        "alert_events",
        "coupons",
        ["coupon_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_alert_events_store_id",
        "alert_events",
        "stores",
        ["store_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f("ix_alert_events_batch_id"), "alert_events", ["batch_id"], unique=False)

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
    op.create_index(
        op.f("ix_deal_alert_marks_user_id"), "deal_alert_marks", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_deal_alert_marks_coupon_id"), "deal_alert_marks", ["coupon_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_deal_alert_marks_coupon_id"), table_name="deal_alert_marks")
    op.drop_index(op.f("ix_deal_alert_marks_user_id"), table_name="deal_alert_marks")
    op.drop_table("deal_alert_marks")

    # Rows with no tracked product are deal alerts; the old schema cannot hold them.
    op.execute("DELETE FROM alert_events WHERE tracked_product_id IS NULL")
    op.drop_index(op.f("ix_alert_events_batch_id"), table_name="alert_events")
    op.drop_constraint("fk_alert_events_store_id", "alert_events", type_="foreignkey")
    op.drop_constraint("fk_alert_events_coupon_id", "alert_events", type_="foreignkey")
    op.drop_column("alert_events", "batch_id")
    op.drop_column("alert_events", "store_id")
    op.drop_column("alert_events", "coupon_id")
    op.alter_column("alert_events", "tracked_product_id", existing_type=sa.Uuid(), nullable=False)
    # Postgres cannot remove enum values, so the four new `alert_kind` values stay.
