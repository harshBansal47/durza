"""add coupon regions and payment methods

Revision ID: 5ee40ae51cc1
Revises: 350adf915c8b
Create Date: 2026-10-21
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "5ee40ae51cc1"
down_revision = "350adf915c8b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("coupons", sa.Column("valid_regions", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("coupons", sa.Column("payment_methods", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("coupons", "payment_methods")
    op.drop_column("coupons", "valid_regions")
