"""rename affiliate_programs.metadata to program_metadata

`metadata` is reserved on a SQLAlchemy declarative class — it names the
`MetaData` object the whole registry hangs off, so a mapped attribute of that
name cannot exist. `a1b2c3d4e5f6` created the column anyway and the model had to
work around it, which left the two permanently out of step: autogenerate wanted
to add `program_metadata` and drop `metadata` on every run, so `alembic check`
could never be clean and a real drift in this table would have been invisible
alongside the permanent noise.

Renaming in place rather than add-and-drop keeps the data, and `IF EXISTS` on
both sides means this is safe to apply to a database that never had the column.
Reversible for the same reason.
"""

import sqlalchemy as sa
from alembic import op

revision = "b3c4d5e6f7a8"
down_revision = "aa11bb22cc33"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "affiliate_programs",
        "metadata",
        new_column_name="program_metadata",
        existing_type=sa.JSON(),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "affiliate_programs",
        "program_metadata",
        new_column_name="metadata",
        existing_type=sa.JSON(),
        existing_nullable=True,
    )
