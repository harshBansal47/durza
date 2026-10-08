"""add affiliate monetization tables

Revision ID: a1b2c3d4e5f6
Revises: 7b3f1a9c4d21
Create Date: 2026-10-04 17:30:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = "7b3f1a9c4d21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- affiliate_providers ----
    op.create_table(
        "affiliate_providers",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=140), nullable=False),
        sa.Column(
            "provider_type",
            sa.Enum("cpa", "cpc", "hybrid", name="provider_type", native_enum=True),
            server_default="cpa",
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column(
            "countries",
            postgresql.ARRAY(sa.String(length=2)),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "api_status",
            sa.Enum(
                "active", "degraded", "paused", "unconfigured",
                name="provider_status", native_enum=True,
            ),
            server_default="unconfigured",
            nullable=False,
        ),
        sa.Column(
            "tracking_status",
            sa.Enum(
                "active", "degraded", "paused", "unconfigured",
                name="provider_tracking_status", native_enum=True,
            ),
            server_default="unconfigured",
            nullable=False,
        ),
        sa.Column("credentials_reference", sa.String(length=120), nullable=True),
        sa.Column("subid_supported", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deeplink_supported", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("postback_supported", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("default_cookie_days", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_affiliate_provider_slug"),
    )
    op.create_index("ix_affiliate_providers_slug", "affiliate_providers", ["slug"], unique=True)

    # ---- affiliate_programs ----
    op.create_table(
        "affiliate_programs",
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("external_program_id", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "active", "pending", "paused", "terminated",
                name="program_status", native_enum=True,
            ),
            server_default="active",
            nullable=False,
        ),
        sa.Column("country", sa.String(length=2), nullable=True),
        sa.Column(
            "commission_type",
            sa.Enum(
                "percentage", "fixed", "tiered",
                name="commission_type", native_enum=True,
            ),
            server_default="percentage",
            nullable=False,
        ),
        sa.Column("commission_value", sa.Numeric(precision=10, scale=4, asdecimal=False), nullable=False, server_default="0.0"),
        sa.Column("cookie_duration_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("deeplink_supported", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("tracking_supported", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["merchant_id"], ["stores.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["provider_id"], ["affiliate_providers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("merchant_id", "provider_id", "country", name="uq_affiliate_program"),
    )
    op.create_index("ix_affiliate_programs_merchant_status", "affiliate_programs", ["merchant_id", "status"])
    op.create_index("ix_affiliate_programs_provider_status", "affiliate_programs", ["provider_id", "status"])

    # ---- affiliate_destinations ----
    op.create_table(
        "affiliate_destinations",
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("program_id", sa.Uuid(), nullable=False),
        sa.Column("destination_url", sa.String(length=1000), nullable=False),
        sa.Column("tracking_url", sa.String(length=2000), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "active", "paused", "invalid",
                name="destination_status", native_enum=True,
            ),
            server_default="active",
            nullable=False,
        ),
        sa.Column("country", sa.String(length=2), nullable=True),
        sa.Column("deep_link", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["merchant_id"], ["stores.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["program_id"], ["affiliate_programs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_affiliate_destinations_merchant_program", "affiliate_destinations", ["merchant_id", "program_id"])
    op.create_index("ix_affiliate_destinations_merchant_status", "affiliate_destinations", ["merchant_id", "status"])


def downgrade() -> None:
    # Every table is dropped before any type is. Postgres refuses to drop a type
    # a live column still depends on, so dropping each type immediately after its
    # own table only works while nothing else shares it — which makes the
    # migration silently order-dependent on a property of the schema that is not
    # visible here. Dropping all the tables first is correct regardless.
    op.drop_index("ix_affiliate_destinations_merchant_status", table_name="affiliate_destinations")
    op.drop_index("ix_affiliate_destinations_merchant_program", table_name="affiliate_destinations")
    op.drop_table("affiliate_destinations")

    op.drop_index("ix_affiliate_programs_provider_status", table_name="affiliate_programs")
    op.drop_index("ix_affiliate_programs_merchant_status", table_name="affiliate_programs")
    op.drop_table("affiliate_programs")

    op.drop_index("ix_affiliate_providers_slug", table_name="affiliate_providers")
    op.drop_table("affiliate_providers")

    op.execute("DROP TYPE IF EXISTS destination_status")
    op.execute("DROP TYPE IF EXISTS commission_type")
    op.execute("DROP TYPE IF EXISTS program_status")
    op.execute("DROP TYPE IF EXISTS provider_tracking_status")
    op.execute("DROP TYPE IF EXISTS provider_status")
    op.execute("DROP TYPE IF EXISTS provider_type")
