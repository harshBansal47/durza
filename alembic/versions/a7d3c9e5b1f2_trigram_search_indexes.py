"""trigram indexes for ILIKE search, and a partial index for the newest-offers feed

Revision ID: a7d3c9e5b1f2
Revises: d1f6c2a8e7b4
Create Date: 2026-11-03

Every storefront search is `column ILIKE '%term%'`. A leading wildcard cannot use a
btree index, so each search was a sequential scan of `coupons` (measured at roughly
0.5 s per statement on 150k rows, twice per request: the count and the page).
`pg_trgm` GIN indexes make that predicate indexable.

Built CONCURRENTLY so a deploy never holds a write lock on the live offers table, which
is why it runs in an autocommit block. IF NOT EXISTS keeps a re-run (or a half-finished
earlier attempt that left an INVALID index behind, see downgrade) from failing the deploy.

Postgres only: the test suite runs on SQLite, where none of this exists.
"""

from alembic import op

revision = "a7d3c9e5b1f2"
down_revision = "d1f6c2a8e7b4"
branch_labels = None
depends_on = None

_TRIGRAM_INDEXES = (
    ("ix_coupons_title_trgm", "coupons", "title"),
    ("ix_coupons_code_trgm", "coupons", "code"),
    ("ix_coupons_description_trgm", "coupons", "description"),
    ("ix_stores_name_trgm", "stores", "name"),
    ("ix_categories_name_trgm", "categories", "name"),
    ("ix_products_name_trgm", "products", "name"),
)

# The default listing is "newest active offers first". Without this the planner scans the
# whole table and sorts it. Partial, because inactive rows are never listed.
#
# The predicate is spelled `IS TRUE` on purpose: that is how SQLAlchemy renders
# `Coupon.is_active.is_(True)`, and Postgres does not match `col IS TRUE` against a partial
# index declared `WHERE col`, so the bare form is silently never used (measured: 73 ms with
# the bare predicate ignored, 0.07 ms once the two spellings agree).
_ACTIVE_NEWEST = "ix_coupons_active_created_at"


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        for name, table, column in _TRIGRAM_INDEXES:
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} "
                f"ON {table} USING gin ({column} gin_trgm_ops)"
            )
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_ACTIVE_NEWEST} "
            "ON coupons (created_at DESC) WHERE is_active IS TRUE"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        for name, _table, _column in _TRIGRAM_INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_ACTIVE_NEWEST}")
        # The pg_trgm extension is left installed: other objects may come to depend on it,
        # and dropping an extension is not reversible by this migration's upgrade alone.
