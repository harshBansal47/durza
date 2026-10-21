"""reconcile schema drift after phase 5

Revision ID: b1c2d3e4f5a6
Revises: 5ee40ae51cc1
Create Date: 2026-10-21
"""

import sqlalchemy as sa

from alembic import op

revision = "b1c2d3e4f5a6"
down_revision = "5ee40ae51cc1"
branch_labels = None
depends_on = None

# Tables whose created_at/updated_at were added without a server default by the
# migrations that created them. The models have always declared `server_default
# now()`, so the database is the side that drifted.
_TIMESTAMP_TABLES = (
    "coupon_staging",
    "daily_content_stats",
    "sale_series",
    "sale_editions",
    "sale_edition_coupon_rules",
    "sale_slug_history",
    "user_interests",
    "user_segments",
)

_ADDED_INDEXES = (
    ("ix_coupons_quality_score_v2", "coupons", ["quality_score_v2"]),
    ("ix_sale_series_publication_status", "sale_series", ["publication_status"]),
    ("ix_sale_editions_publication_status", "sale_editions", ["publication_status"]),
    ("ix_sale_editions_editorial_content_id", "sale_editions", ["editorial_content_id"]),
)


def _check_no_orphans() -> None:
    """Fail loudly before adding FKs that would reject existing rows."""
    conn = op.get_bind()
    orphan_sources = conn.execute(
        sa.text(
            """
            SELECT count(*)
            FROM coupon_staging AS cs
            LEFT JOIN sources AS s ON s.id = cs.source_id
            WHERE s.id IS NULL
            """
        )
    ).scalar_one()
    if orphan_sources:
        raise RuntimeError(
            f"coupon_staging has {orphan_sources} row(s) with a source_id that "
            "does not exist in sources; fix the orphans before adding the FK."
        )

    orphan_stores = conn.execute(
        sa.text(
            """
            SELECT count(*)
            FROM coupon_staging AS cs
            LEFT JOIN stores AS s ON s.id = cs.store_id
            WHERE cs.store_id IS NOT NULL AND s.id IS NULL
            """
        )
    ).scalar_one()
    if orphan_stores:
        raise RuntimeError(
            f"coupon_staging has {orphan_stores} row(s) with a store_id that "
            "does not exist in stores; fix the orphans before adding the FK."
        )


def upgrade() -> None:
    _check_no_orphans()

    # NOT VALID then VALIDATE keeps the table available while Postgres checks
    # existing rows. The orphan check above means VALIDATE should succeed.
    op.execute(
        "ALTER TABLE coupon_staging "
        "ADD CONSTRAINT fk_coupon_staging_source_id "
        "FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE NOT VALID"
    )
    op.execute("ALTER TABLE coupon_staging VALIDATE CONSTRAINT fk_coupon_staging_source_id")
    op.execute(
        "ALTER TABLE coupon_staging "
        "ADD CONSTRAINT fk_coupon_staging_store_id "
        "FOREIGN KEY (store_id) REFERENCES stores(id) ON DELETE SET NULL NOT VALID"
    )
    op.execute("ALTER TABLE coupon_staging VALIDATE CONSTRAINT fk_coupon_staging_store_id")

    for table in _TIMESTAMP_TABLES:
        for column in ("created_at", "updated_at"):
            op.alter_column(
                table,
                column,
                existing_type=sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                existing_nullable=False,
            )

    for name, table, columns in _ADDED_INDEXES:
        op.create_index(name, table, columns)


def downgrade() -> None:
    for name, table, _columns in reversed(_ADDED_INDEXES):
        op.drop_index(name, table_name=table)

    for table in reversed(_TIMESTAMP_TABLES):
        for column in ("updated_at", "created_at"):
            op.alter_column(
                table,
                column,
                existing_type=sa.DateTime(timezone=True),
                server_default=None,
                existing_nullable=False,
            )

    op.drop_constraint("fk_coupon_staging_store_id", "coupon_staging", type_="foreignkey")
    op.drop_constraint("fk_coupon_staging_source_id", "coupon_staging", type_="foreignkey")
