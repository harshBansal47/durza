"""add content_ref to click_events

Revision ID: d06c8ddc0123
Revises: d5cb21a2cc57
Create Date: 2026-10-21
"""
from alembic import op
import sqlalchemy as sa

revision = "d06c8ddc0123"
down_revision = "d5cb21a2cc57"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("click_events", sa.Column("content_ref", sa.String(length=80), nullable=True))


def downgrade() -> None:
    op.drop_column("click_events", "content_ref")
