"""alert_event_delivered_flag

Revision ID: fd8ad304426a
Revises: c4d8f2a91b60
Create Date: 2026-10-02 23:10:03.881269

"""

import sqlalchemy as sa

from alembic import op

revision = "fd8ad304426a"
down_revision = "c4d8f2a91b60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default is required, not decorative: adding a NOT NULL column to a
    # table that already has rows fails without one. Existing events predate the
    # flag, so we cannot know whether they were delivered. `true` is the honest
    # reading of "we recorded this row after attempting the send" and avoids
    # retroactively accusing the past of having failed.
    op.add_column(
        "alert_events",
        sa.Column("delivered", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.alter_column("alert_events", "delivered", server_default=None)


def downgrade() -> None:
    op.drop_column("alert_events", "delivered")
