"""telegram_posts: the once-only ledger for the Telegram channel

Revision ID: d9e5a7c3b2f4
Revises: c8d4f6b2a1e3
Create Date: 2026-11-01 10:00:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "d9e5a7c3b2f4"
down_revision = "c8d4f6b2a1e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_posts",
        sa.Column("coupon_id", sa.Uuid(), nullable=True),
        sa.Column("dedupe_key", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("chat_id", sa.String(length=64), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("store_name", sa.String(length=150), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("error", sa.String(length=300), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("coupon_id", name="uq_telegram_posts_coupon_id"),
        sa.UniqueConstraint("dedupe_key", name="uq_telegram_posts_dedupe_key"),
    )


def downgrade() -> None:
    op.drop_table("telegram_posts")
