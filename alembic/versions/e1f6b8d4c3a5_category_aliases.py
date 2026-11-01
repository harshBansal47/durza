"""category_aliases: provider category wording mapped onto our taxonomy

Revision ID: e1f6b8d4c3a5
Revises: d9e5a7c3b2f4
Create Date: 2026-11-02 10:00:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "e1f6b8d4c3a5"
down_revision = "d9e5a7c3b2f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "category_aliases",
        sa.Column("alias", sa.String(length=160), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=True),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("seen_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_source", sa.String(length=80), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("alias", name="uq_category_aliases_alias"),
    )


def downgrade() -> None:
    op.drop_table("category_aliases")
