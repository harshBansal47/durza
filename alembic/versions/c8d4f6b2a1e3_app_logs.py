"""app_logs: a searchable application log that expires on its own

Revision ID: c8d4f6b2a1e3
Revises: b7c3e5a1d9f2
Create Date: 2026-10-30 12:00:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "c8d4f6b2a1e3"
down_revision = "b7c3e5a1d9f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("level", sa.String(length=10), nullable=False),
        sa.Column("logger", sa.String(length=100), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("fields", sa.JSON(), nullable=True),
        sa.Column("exception", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_app_logs_created_at"), "app_logs", ["created_at"], unique=False)
    op.create_index(op.f("ix_app_logs_request_id"), "app_logs", ["request_id"], unique=False)
    op.create_index(
        "ix_app_logs_level_created_at", "app_logs", ["level", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_app_logs_level_created_at", table_name="app_logs")
    op.drop_index(op.f("ix_app_logs_request_id"), table_name="app_logs")
    op.drop_index(op.f("ix_app_logs_created_at"), table_name="app_logs")
    op.drop_table("app_logs")
