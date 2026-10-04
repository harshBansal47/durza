"""add fraud_score to click events

Revision ID: aa11bb22cc33
Revises: f6a7b8c9d0e1
Create Date: 2026-10-04 21:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = "aa11bb22cc33"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("click_events", sa.Column("fraud_score", sa.Float(), server_default="0.0", nullable=False))


def downgrade() -> None:
    op.drop_column("click_events", "fraud_score")
