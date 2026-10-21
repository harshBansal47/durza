"""make coupon verifications' ip hash nullable

Revision ID: f7a8b9c0d1e2
Revises: b1c2d3e4f5a6
Create Date: 2026-10-22
"""

import sqlalchemy as sa

from alembic import op

revision = "f7a8b9c0d1e2"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The hash of a voter's IP only rate-limits a repeat vote. Once the vote
    # is older than the retention window the hash is scrubbed to NULL, so the
    # column has to allow it. The vote itself — its verdict and its note —
    # stays.
    op.alter_column(
        "coupon_verifications",
        "ip_hash",
        existing_type=sa.String(length=64),
        nullable=True,
    )


def downgrade() -> None:
    # Reverting would fail on any row that has already been scrubbed, so the
    # rows are backfilled with a placeholder rather than left NULL.
    op.execute("UPDATE coupon_verifications SET ip_hash = 'scrubbed' WHERE ip_hash IS NULL")
    op.alter_column(
        "coupon_verifications",
        "ip_hash",
        existing_type=sa.String(length=64),
        nullable=False,
    )
