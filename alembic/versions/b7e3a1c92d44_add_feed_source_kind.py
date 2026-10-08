"""add 'feed' to source_kind enum

Revision ID: b7e3a1c92d44
Revises: 1f2b1639c23d
Create Date: 2026-10-02 15:00:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "b7e3a1c92d44"
down_revision = "1f2b1639c23d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE source_kind ADD VALUE IF NOT EXISTS 'feed'")
    # SQLite stores the enum as VARCHAR, so there is nothing to alter there.


def downgrade() -> None:
    # Postgres cannot drop a single enum value without recreating the type;
    # leaving 'feed' in place is harmless on downgrade.
    pass
