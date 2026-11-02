"""remove private Telegram notification preferences

Revision ID: a2c7e1f904bd
Revises: f2a8c1d4e6b9
Create Date: 2026-11-02

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "a2c7e1f904bd"
down_revision = "f2a8c1d4e6b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("notification_preferences", "telegram_chat_id")
    op.drop_column("notification_preferences", "telegram_enabled")


def downgrade() -> None:
    op.add_column(
        "notification_preferences",
        sa.Column("telegram_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("notification_preferences", "telegram_enabled", server_default=None)
    op.add_column(
        "notification_preferences",
        sa.Column("telegram_chat_id", sa.String(length=64), nullable=True),
    )
