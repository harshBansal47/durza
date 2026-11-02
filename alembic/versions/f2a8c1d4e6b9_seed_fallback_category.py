"""Seed the default ingestion fallback category.

Revision ID: f2a8c1d4e6b9
Revises: e1f6b8d4c3a5
Create Date: 2026-11-02 11:40:00.000000
"""

import sqlalchemy as sa

from alembic import op

revision = "f2a8c1d4e6b9"
down_revision = "e1f6b8d4c3a5"
branch_labels = None
depends_on = None

_CATEGORY_ID = "f2a8c1d4-e6b9-4c2a-8f10-000000000001"


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            "INSERT INTO categories (id, name, slug) "
            "VALUES (:id, 'Other', 'other') "
            "ON CONFLICT (slug) DO NOTHING"
        ),
        {"id": _CATEGORY_ID},
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text("DELETE FROM categories WHERE slug = 'other' AND id = :id"),
        {"id": _CATEGORY_ID},
    )
