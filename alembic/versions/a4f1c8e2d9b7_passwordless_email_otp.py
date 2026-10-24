"""replace password reset tokens with passwordless email sign-in

Revision ID: a4f1c8e2d9b7
Revises: f7a8b9c0d1e2
Create Date: 2026-10-24

The public product no longer supports password sign-in. Existing staff accounts
keep their `hashed_password` (the /admin login uses it); this migration removes
only the reset-token machinery that has no remaining reader.

`email_verified_at` records that an address was proven by Google or by a
consumed email code. `token_version` is the stateless-logout counter included in
every JWT as `ver`.
"""

import sqlalchemy as sa

from alembic import op

revision = "a4f1c8e2d9b7"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Existing sessions carry no `ver` claim and are treated as version 0, so a
    # server-default of 0 keeps everyone signed in across this deploy. Logging
    # out increments the value from there.
    op.add_column(
        "users",
        sa.Column("token_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )

    op.create_table(
        "email_otp_challenges",
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("request_ip_hash", sa.String(length=64), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_email_otp_challenges_email"), "email_otp_challenges", ["email"], unique=False
    )

    # No endpoint, model, or scheduled job reads this table after the rewrite.
    # Dropping it in the same release avoids a permanently dead table and the
    # autogenerate drift it would cause; `scripts/check_destructive_migrations.py`
    # allowlists this one reviewed removal.
    op.drop_index(op.f("ix_password_reset_tokens_user_id"), table_name="password_reset_tokens")
    op.drop_index(op.f("ix_password_reset_tokens_token_hash"), table_name="password_reset_tokens")
    op.drop_table("password_reset_tokens")


def downgrade() -> None:
    op.create_table(
        "password_reset_tokens",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_password_reset_tokens_token_hash"),
        "password_reset_tokens",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        op.f("ix_password_reset_tokens_user_id"), "password_reset_tokens", ["user_id"], unique=False
    )

    op.drop_index(op.f("ix_email_otp_challenges_email"), table_name="email_otp_challenges")
    op.drop_table("email_otp_challenges")
    op.drop_column("users", "token_version")
    op.drop_column("users", "email_verified_at")
