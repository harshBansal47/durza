"""add quality_score_v2

Revision ID: a2b68934f2c5
Revises: 3075763935f2
Create Date: 2026-10-21
"""
from alembic import op
import sqlalchemy as sa

revision = "a2b68934f2c5"
down_revision = "3075763935f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("coupons", sa.Column("quality_score_v2", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("coupons", "quality_score_v2")
