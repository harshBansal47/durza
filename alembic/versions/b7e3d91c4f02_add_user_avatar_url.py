"""add user avatar url

Revision ID: b7e3d91c4f02
Revises: c9e1a4d27b50
Create Date: 2026-10-18 07:45:00.000000

"""

import sqlalchemy as sa

from alembic import op

revision = "b7e3d91c4f02"
down_revision = "c9e1a4d27b50"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable, and stays nullable: password accounts have no Google photo,
    # and existing rows predate the column entirely. Google sign-ins fill it
    # from the ID token's `picture` claim.
    op.add_column("users", sa.Column("avatar_url", sa.String(length=1024), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "avatar_url")
