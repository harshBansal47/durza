"""add deals impression type

Revision ID: d5cb21a2cc57
Revises: f3a41b7c9d20
Create Date: 2026-10-21
"""
from alembic import op

revision = "d5cb21a2cc57"
down_revision = "f3a41b7c9d20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE impression_type ADD VALUE IF NOT EXISTS 'deals'")


def downgrade() -> None:
    # PostgreSQL cannot drop an enum value in place. Recreating the enum would
    # risk data loss, so this migration is intentionally additive; production
    # rollback should leave the unused value in place.
    pass
