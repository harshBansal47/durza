"""parsed advertiser terms on vCommission campaigns

Revision ID: c3e8a1d7f592
Revises: b6c1d9e2f304
Create Date: 2026-11-06

Adds `terms` (what the advertiser allows and how it pays, parsed from the description at
import time) and `needs_review` (the queryable summary of it). Both are additive and nullable /
defaulted, so existing rows stay valid; `terms` is filled in by the next campaign sync, because
the stored description is flattened to one line and cannot be re-parsed after the fact.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c3e8a1d7f592"
down_revision = "b6c1d9e2f304"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("affiliate_campaigns", sa.Column("terms", _JSON, nullable=True))
    op.add_column(
        "affiliate_campaigns",
        sa.Column("needs_review", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("affiliate_campaigns", "needs_review")
    op.drop_column("affiliate_campaigns", "terms")
