"""add user interests and personalization consent

Revision ID: 566231899a40
Revises: a2b68934f2c5
Create Date: 2026-10-21
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "566231899a40"
down_revision = "a2b68934f2c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "personalization_consent", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.create_table(
        "user_interests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("interest_type", sa.String(length=20), nullable=False),
        sa.Column("interest_key", sa.String(length=120), nullable=False),
        sa.Column("score", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "interest_type", "interest_key", name="uq_user_interest"),
        sa.CheckConstraint(
            "interest_type IN ('category','store','bank')", name="ck_user_interest_type"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_user_interests_user", "user_interests", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_interests_user", table_name="user_interests")
    op.drop_table("user_interests")
    op.drop_column("users", "personalization_consent")
