"""job_runs_and_verification_attempts

Two tables that make the platform's background work observable after the fact.

* ``job_runs`` — durable history of every scheduled job execution. Logs are
  rotated; this is what answers "did ingestion run at 04:00, and if not, when
  did it last succeed?"
* ``verification_attempts`` — our own automated re-checks of coupon codes, kept
  deliberately separate from the human ``coupon_verifications`` table so a flaky
  checker can never drag down a community success rate shown to shoppers.

Revision ID: 064bf0412cdd
Revises: fd8ad304426a
Create Date: 2026-10-02 23:18:46.601561

"""

import sqlalchemy as sa

from alembic import op

revision = "064bf0412cdd"
down_revision = "fd8ad304426a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_runs",
        sa.Column("job", sa.String(length=60), nullable=False),
        sa.Column(
            "status",
            sa.Enum("running", "success", "failed", "skipped", name="job_status"),
            nullable=False,
        ),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
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
    op.create_index(op.f("ix_job_runs_job"), "job_runs", ["job"], unique=False)
    op.create_index(op.f("ix_job_runs_started_at"), "job_runs", ["started_at"], unique=False)

    op.create_table(
        "verification_attempts",
        sa.Column("coupon_id", sa.Uuid(), nullable=False),
        sa.Column("checker", sa.String(length=60), nullable=False),
        sa.Column(
            "outcome",
            sa.Enum("worked", "failed", "inconclusive", name="attempt_outcome"),
            nullable=False,
        ),
        # Nullable on purpose: "we could not check" is not "we checked and it
        # failed", and a NOT NULL boolean here would force us to conflate them.
        sa.Column("valid", sa.Boolean(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["coupon_id"], ["coupons.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_verification_attempt_coupon_checker",
        "verification_attempts",
        ["coupon_id", "checker", "checked_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_verification_attempts_checked_at"),
        "verification_attempts",
        ["checked_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_verification_attempts_checker"),
        "verification_attempts",
        ["checker"],
        unique=False,
    )
    op.create_index(
        op.f("ix_verification_attempts_coupon_id"),
        "verification_attempts",
        ["coupon_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_verification_attempts_coupon_id"), table_name="verification_attempts")
    op.drop_index(op.f("ix_verification_attempts_checker"), table_name="verification_attempts")
    op.drop_index(op.f("ix_verification_attempts_checked_at"), table_name="verification_attempts")
    op.drop_index("ix_verification_attempt_coupon_checker", table_name="verification_attempts")
    op.drop_table("verification_attempts")
    op.drop_index(op.f("ix_job_runs_started_at"), table_name="job_runs")
    op.drop_index(op.f("ix_job_runs_job"), table_name="job_runs")
    op.drop_table("job_runs")
    # Native enum types are not owned by the table the way an index or a
    # constraint is: `op.drop_table` removes the column that used the type and
    # leaves the type itself behind. Alembic does not track types created inline
    # inside `op.create_table`, so nothing drops them automatically.
    #
    # The consequence of omitting these lines is not a cosmetic leftover. On the
    # next `alembic upgrade head` the upgrade fails with
    # `type "ad_position" already exists`, which means a deploy cannot be rolled
    # back and then re-applied — the single most expensive thing a migration can
    # fail to do. `IF EXISTS` keeps the downgrade safe against a database where
    # the type was never created (SQLite, where these are plain VARCHARs).

    op.execute("DROP TYPE IF EXISTS attempt_outcome")
    op.execute("DROP TYPE IF EXISTS job_status")
