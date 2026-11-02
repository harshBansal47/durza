"""remove browser push account alerts

Revision ID: 8f4d1c2b6a90
Revises: a2c7e1f904bd
Create Date: 2026-11-02

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "8f4d1c2b6a90"
down_revision = "a2c7e1f904bd"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("notification_preferences", "push_subscription")
    op.drop_column("notification_preferences", "push_enabled")


def downgrade() -> None:
    op.add_column(
        "notification_preferences",
        sa.Column("push_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("notification_preferences", "push_enabled", server_default=None)
    op.add_column(
        "notification_preferences",
        sa.Column("push_subscription", sa.Text(), nullable=True),
    )
