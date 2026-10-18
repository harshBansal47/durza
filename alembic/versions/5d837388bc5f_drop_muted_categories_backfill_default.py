"""drop muted_categories backfill server default

Revision ID: 5d837388bc5f
Revises: d3e5f7a9b1c3
Create Date: 2026-10-18 23:25:55.109481

"""

import sqlalchemy as sa

from alembic import op

revision = "5d837388bc5f"
down_revision = "d3e5f7a9b1c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # c2f4 added muted_categories with server_default '[]' so existing rows were
    # valid while the NOT NULL column was attached. The model carries only a
    # Python-side default, so the database-side one is drift `alembic check`
    # rejects. This is the same cleanup `alert_events.delivered` (fd8ad) does
    # right after its backfill default; c2f4 simply missed it.
    op.alter_column(
        "notification_preferences",
        "muted_categories",
        existing_type=sa.JSON(),
        server_default=None,
        existing_nullable=False,
    )


def downgrade() -> None:
    # Restore the default exactly as c2f4 created it, so the revision can be
    # re-applied without another schema edit.
    op.alter_column(
        "notification_preferences",
        "muted_categories",
        existing_type=sa.JSON(),
        server_default=sa.text("'[]'::json"),
        existing_nullable=False,
    )
