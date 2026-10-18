"""add muted_categories to notification_preferences

Revision ID: c2f4a1b3e5d7
Revises: b7e3d91c4f02
Create Date: 2026-10-18 09:00:00.000000

"""

import sqlalchemy as sa

from alembic import op

revision = "c2f4a1b3e5d7"
down_revision = "b7e3d91c4f02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default keeps existing rows valid (empty list = nothing muted).
    op.add_column(
        "notification_preferences",
        sa.Column(
            "muted_categories",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("notification_preferences", "muted_categories")
