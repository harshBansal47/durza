"""make users.hashed_password nullable for google-only accounts

Revision ID: d3e5f7a9b1c3
Revises: c2f4a1b3e5d7
Create Date: 2026-10-18 09:30:00.000000

"""

import sqlalchemy as sa

from alembic import op

revision = "d3e5f7a9b1c3"
down_revision = "c2f4a1b3e5d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Google-only accounts have no password, so this column must accept NULL.
    # Existing rows keep their values; only the constraint changes.
    op.alter_column("users", "hashed_password", existing_type=sa.String(length=255), nullable=True)


def downgrade() -> None:
    # Re-tightening is only safe if no NULL rows exist; the downgrade does not
    # attempt to backfill, so it may fail on a populated database.
    op.alter_column("users", "hashed_password", existing_type=sa.String(length=255), nullable=False)
