"""add coupon staging and quarantine

Revision ID: 3075763935f2
Revises: 955c936b8912
Create Date: 2026-10-21
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "3075763935f2"
down_revision = "955c936b8912"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ingestion_runs", sa.Column("quarantined", sa.Integer(), nullable=False, server_default="0"))
    op.create_table(
        "coupon_staging",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("store_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="quarantined"),
        sa.Column("reason_code", sa.String(length=60), nullable=False),
        sa.Column("observed_value", sa.String(length=120), nullable=True),
        sa.Column("threshold_value", sa.String(length=120), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.String(length=160), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "external_id", "content_hash", name="uq_coupon_staging_dedupe"),
        sa.CheckConstraint("status IN ('quarantined','approved','rejected','expired')", name="ck_coupon_staging_status"),
    )
    op.create_index("ix_coupon_staging_source", "coupon_staging", ["source_id"])


def downgrade() -> None:
    op.drop_index("ix_coupon_staging_source", table_name="coupon_staging")
    op.drop_table("coupon_staging")
    op.drop_column("ingestion_runs", "quarantined")
