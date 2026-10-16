"""multi-source core: coupon_sources, merchant identity, source precedence

Revision ID: 7b3f1a9c4d21
Revises: d7a2b5c8e913
Create Date: 2026-10-04

Replaces the discarded revision c21f9807176e, which generated a `drop_table`
for `conversions` because the new CouponSource model temporarily lived in
app/models/conversion.py and Alembic read the swap as a rename. Anything that
applied it needs scripts/repair_dropped_conversions.sql first.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "7b3f1a9c4d21"
down_revision = "d7a2b5c8e913"
branch_labels = None
depends_on = None


def _add_enum_values(enum_name: str, *values: str) -> None:
    """Add labels to an existing Postgres enum, skipping ones already present.

    `ALTER TYPE ... ADD VALUE` is not idempotent, so the existence check makes a
    re-run a no-op instead of an error. Postgres 12+ accepts this inside a
    transaction, which matters: committing part-way through would leave the
    migration half-applied with no way to know how far it got.

    This is what the empty-bodied `ef06a153c119 add awin source kind` should
    have contained. Because it did not, `source_kind` in every deployed
    database still lacks `awin`, and the first insert of an Awin source row
    fails on an invalid enum value — the network the whole project is built
    around, unusable.
    """
    conn = op.get_bind()
    for value in values:
        exists = conn.execute(
            sa.text(
                "SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = :type_name AND e.enumlabel = :value"
            ),
            {"type_name": enum_name, "value": value},
        ).scalar()
        if exists:
            continue
        op.execute(sa.text(f"ALTER TYPE {enum_name} ADD VALUE '{value}'"))


def upgrade() -> None:
    # ---- New enum types, created before the columns that use them ----------
    postgresql.ENUM("active", "pending", "inactive", "merged", name="store_status").create(
        op.get_bind(), checkfirst=True
    )
    postgresql.ENUM(
        "unverified",
        "pending",
        "verified",
        "disputed",
        "failed",
        name="verification_status",
    ).create(op.get_bind(), checkfirst=True)
    postgresql.ENUM("incremental", "full", name="sync_mode").create(op.get_bind(), checkfirst=True)
    postgresql.ENUM(
        "healthy", "degraded", "failing", "paused", "unconfigured", name="source_status"
    ).create(op.get_bind(), checkfirst=True)
    postgresql.ENUM(
        "active", "inactive", "expired", "failed", "deleted", name="coupon_source_status"
    ).create(op.get_bind(), checkfirst=True)

    # ---- Enum labels -------------------------------------------------------
    _add_enum_values("source_kind", "awin", "rakuten", "cj", "admitad", "impact")
    _add_enum_values(
        "coupon_status",
        "pending_validation",
        "validation_failed",
        "source_removed",
        "disabled",
    )
    _add_enum_values("run_status", "partial", "cancelled")

    # ---- coupon_sources ----------------------------------------------------
    # One row per (canonical coupon, provider). This is the table that lets four
    # networks advertise the same code without the shopper seeing it four times.
    op.create_table(
        "coupon_sources",
        sa.Column("coupon_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("affiliate_url", sa.Text(), nullable=True),
        sa.Column(
            "status",
            # create_type=False: the type was created above, and letting the
            # column create it again is an "already exists" failure.
            postgresql.ENUM(
                "active",
                "inactive",
                "expired",
                "failed",
                "deleted",
                name="coupon_source_status",
                create_type=False,
            ),
            nullable=False,
            server_default="active",
        ),
        # The provider's own expiry claim, kept apart from coupons.expires_at so
        # conflict resolution can compare the two without parsing raw_data.
        sa.Column("source_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_title", sa.String(length=300), nullable=True),
        sa.Column("raw_data", sa.JSON(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["coupon_id"], ["coupons.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # One row per (coupon, source) — this is what makes the link upsertable.
        sa.UniqueConstraint("coupon_id", "source_id", name="uq_coupon_source"),
    )
    # Identity path during ingestion: look up by the provider's own id.
    op.create_index(
        "ix_coupon_sources_source_external", "coupon_sources", ["source_id", "external_id"]
    )
    # "Every source backing this coupon" — coupon detail page and AffiliateRouter.
    # Composed with status so the router can ask "active sources only" from the
    # same index instead of filtering after the fact.
    op.create_index("ix_coupon_sources_coupon_status", "coupon_sources", ["coupon_id", "status"])

    # ---- stores: canonical merchant identity -------------------------------
    op.add_column("stores", sa.Column("normalized_name", sa.String(length=160), nullable=True))
    op.add_column("stores", sa.Column("normalized_domain", sa.String(length=190), nullable=True))
    op.add_column("stores", sa.Column("aliases", sa.ARRAY(sa.String(length=160)), nullable=True))
    op.add_column(
        "stores",
        sa.Column(
            "status",
            postgresql.ENUM(
                "active", "pending", "inactive", "merged", name="store_status", create_type=False
            ),
            server_default="active",
            nullable=False,
        ),
    )
    op.add_column("stores", sa.Column("merged_into_id", sa.String(length=36), nullable=True))

    # Backfill so already-ingested coupons gain a usable identity immediately.
    # Existing stores only have a slug and often no website_url, so the name is
    # the only signal available; a real domain arrives on the next ingestion run.
    op.execute(
        "UPDATE stores SET normalized_name = lower(regexp_replace(name, '[^a-zA-Z0-9]+', '', 'g')) "
        "WHERE normalized_name IS NULL"
    )
    op.execute("UPDATE stores SET normalized_name = lower(slug) WHERE normalized_name IS NULL")
    op.execute("UPDATE stores SET aliases = '{}' WHERE aliases IS NULL")
    op.alter_column("stores", "normalized_name", nullable=False)
    op.alter_column("stores", "aliases", nullable=False)

    # A NULL domain must not collide: providers that give a name but no usable
    # domain leave it NULL, and both Postgres and SQLite treat NULLs as distinct
    # in a UNIQUE constraint. Every store that *has* a domain owns it.
    op.create_unique_constraint("uq_store_normalized_domain", "stores", ["normalized_domain"])
    op.create_index("ix_stores_normalized_name", "stores", ["normalized_name"])

    # ---- sources: precedence, scope, credentials, health -------------------
    op.add_column(
        "sources", sa.Column("priority", sa.Integer(), server_default="100", nullable=False)
    )
    op.add_column(
        "sources", sa.Column("confidence", sa.Float(), server_default="0.5", nullable=False)
    )
    op.add_column("sources", sa.Column("countries", sa.ARRAY(sa.String(length=2)), nullable=True))
    op.add_column("sources", sa.Column("credential_ref", sa.String(length=120), nullable=True))
    op.add_column("sources", sa.Column("rate_limit_per_minute", sa.Integer(), nullable=True))
    op.add_column("sources", sa.Column("requests_per_second", sa.Float(), nullable=True))
    op.add_column(
        "sources",
        sa.Column("sync_interval_minutes", sa.Integer(), server_default="360", nullable=False),
    )
    op.add_column(
        "sources", sa.Column("timeout_seconds", sa.Float(), server_default="60.0", nullable=False)
    )
    # Column is `api_status`, not `status`. A Source has no lifecycle of its own —
    # only the health of the upstream API it calls — so `status` would read as
    # "is this source on", which `is_enabled` already means, and invite someone
    # to conflate a flaky provider with a switched-off one.
    op.add_column(
        "sources",
        sa.Column(
            "api_status",
            postgresql.ENUM(
                "healthy",
                "degraded",
                "failing",
                "paused",
                "unconfigured",
                name="source_status",
                create_type=False,
            ),
            server_default="healthy",
            nullable=False,
        ),
    )
    op.add_column(
        "sources",
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("sources", sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "sources", sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "sources", sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("sources", sa.Column("last_error", sa.Text(), nullable=True))
    op.execute("UPDATE sources SET countries = '{}' WHERE countries IS NULL")
    op.alter_column("sources", "countries", nullable=False)

    # ---- coupons: trust, ranking, provenance -------------------------------
    op.add_column("coupons", sa.Column("normalized_code", sa.String(length=50), nullable=True))
    op.add_column("coupons", sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "coupons",
        sa.Column(
            "verification_status",
            postgresql.ENUM(
                "unverified",
                "pending",
                "verified",
                "disputed",
                "failed",
                name="verification_status",
                create_type=False,
            ),
            server_default="unverified",
            nullable=False,
        ),
    )
    op.add_column(
        "coupons", sa.Column("quality_score", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column("coupons", sa.Column("country", sa.String(length=2), nullable=True))
    op.add_column("coupons", sa.Column("restrictions", sa.JSON(), nullable=True))
    op.add_column(
        "coupons", sa.Column("source_categories", sa.ARRAY(sa.String(length=80)), nullable=True)
    )
    op.add_column(
        "coupons", sa.Column("source_count", sa.Integer(), server_default="0", nullable=False)
    )

    # Existing codes are canonicalised now so the dedup index is usable for
    # coupons ingested before this migration.
    op.execute(
        "UPDATE coupons SET normalized_code = upper(regexp_replace(code, '[^A-Za-z0-9]+', '', 'g')) "
        "WHERE code IS NOT NULL AND normalized_code IS NULL"
    )
    op.execute("UPDATE coupons SET source_categories = '{}' WHERE source_categories IS NULL")
    op.alter_column("coupons", "source_categories", nullable=False)
    op.create_index("ix_coupons_normalized_code", "coupons", ["normalized_code"])
    op.create_index("ix_coupons_country", "coupons", ["country"])
    # The "Recommended" sort orders by this; without the index a popular listing
    # degenerates into a full scan plus sort on every page load.
    op.create_index("ix_coupons_quality_score", "coupons", ["quality_score"])

    # ---- ingestion_runs: honest counters -----------------------------------
    op.add_column(
        "ingestion_runs",
        sa.Column(
            "mode",
            postgresql.ENUM("incremental", "full", name="sync_mode", create_type=False),
            server_default="incremental",
            nullable=False,
        ),
    )
    op.add_column(
        "ingestion_runs",
        sa.Column("duplicated", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "ingestion_runs",
        sa.Column("malformed", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "ingestion_runs",
        sa.Column("expired", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("ingestion_runs", sa.Column("duration_ms", sa.Integer(), nullable=True))
    op.add_column(
        "ingestion_runs",
        sa.Column("pages_fetched", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "ingestion_runs",
        sa.Column("cancel_requested", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("ingestion_runs", sa.Column("triggered_by", sa.String(length=80), nullable=True))

    # `skipped` was never written by the pipeline and said nothing about why a
    # record did not become a coupon. The specific counters above replace it.
    op.drop_column("ingestion_runs", "skipped")


def downgrade() -> None:
    op.add_column(
        "ingestion_runs",
        sa.Column("skipped", sa.Integer(), server_default="0", nullable=False),
    )
    for column in (
        "triggered_by",
        "cancel_requested",
        "pages_fetched",
        "duration_ms",
        "expired",
        "malformed",
        "duplicated",
        "mode",
    ):
        op.drop_column("ingestion_runs", column)

    op.drop_index("ix_coupons_country", table_name="coupons")
    op.drop_index("ix_coupons_normalized_code", table_name="coupons")
    op.drop_index("ix_coupons_quality_score", table_name="coupons")
    for column in (
        "source_count",
        "source_categories",
        "restrictions",
        "country",
        "quality_score",
        "verification_status",
        "starts_at",
        "normalized_code",
    ):
        op.drop_column("coupons", column)

    for column in (
        "last_error",
        "last_failure_at",
        "last_success_at",
        "last_sync_at",
        "consecutive_failures",
        "api_status",
        "timeout_seconds",
        "sync_interval_minutes",
        "requests_per_second",
        "rate_limit_per_minute",
        "credential_ref",
        "countries",
        "confidence",
        "priority",
    ):
        op.drop_column("sources", column)

    op.drop_index("ix_stores_normalized_name", table_name="stores")
    op.drop_constraint("uq_store_normalized_domain", "stores", type_="unique")
    for column in (
        "merged_into_id",
        "status",
        "aliases",
        "normalized_domain",
        "normalized_name",
    ):
        op.drop_column("stores", column)

    op.drop_index("ix_coupon_sources_coupon_status", table_name="coupon_sources")
    op.drop_index("ix_coupon_sources_source_external", table_name="coupon_sources")
    op.drop_table("coupon_sources")

    # Types created outside create_table are still not owned by it, so
    # drop_table leaves them behind and the next upgrade fails with
    # `<type> already exists`. Literal names, no interpolation.
    op.execute("DROP TYPE IF EXISTS coupon_source_status")
    op.execute("DROP TYPE IF EXISTS source_status")
    op.execute("DROP TYPE IF EXISTS sync_mode")
    op.execute("DROP TYPE IF EXISTS verification_status")
    op.execute("DROP TYPE IF EXISTS store_status")

    # Labels added to `source_kind`, `coupon_status` and `run_status` cannot be
    # removed — Postgres has no DROP VALUE. The widened enums are a superset of
    # what the previous revision allowed, so a re-upgrade finds every label it
    # needs already present and the round-trip is safe.
