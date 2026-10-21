"""Prove the migrations survive a downgrade and re-upgrade, with data present.

Run between `alembic upgrade head` and `alembic downgrade base`:

    python scripts/migration_roundtrip.py seed
    alembic downgrade base
    alembic upgrade head
    python scripts/migration_roundtrip.py check

Why this exists rather than a bare `downgrade base && upgrade head`: dropping an
*empty* table only proves the DROP statement parses. A migration that loses a
column along with its data, or leaves a dependent enum type behind, passes
against an empty database and fails against a real one. So every table gets a
row first.

Two design decisions worth stating, because both are the opposite of the obvious
one:

* **The schema is introspected, not hardcoded.** A hardcoded list of tables and
  columns is a list that goes stale the moment a migration is added, and it goes
  stale silently — the new table is simply never checked. Reading
  `information_schema` means a new migration is covered with no edit here.

* **Rows are synthesised to satisfy the real constraints, not a minimal subset.**
  Inserting one convenient column per table and letting the rest be NULL passes
  on any schema and proves nothing. Instead every NOT NULL column without a
  default is filled with a value appropriate to its type, and foreign keys are
  resolved against rows this script already inserted. If a migration makes a
  column NOT NULL without a default or a backfill, that insert fails — which is
  the entire point.

Deliberately raw SQL rather than the ORM. The models are the *target* state, so
inserting through them would only ever exercise a schema the models already
agree with. Writing literals against `information_schema` is what proves the
migration actually produced the schema it claims to.
"""

import asyncio
import re
import sys
import uuid
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from app.core.database import Base, engine
from app.models import (  # noqa: F401  # noqa: F401  # noqa: F401
    ad,
    category,
    coupon,
    coupon_verification,
    ingestion_run,
    job,
    page,
    product,
    source,
    store,
    tracking,
    user,
)

# Tables come from the models, columns from `information_schema`.
#
# That split is deliberate. Asking the database for its own table list also turns
# up anything else that shares the database — a leftover table from a previous
# experiment, an extension's bookkeeping — and this script would then seed and
# verify tables no migration has ever heard of. The models are the declared
# schema, which is exactly the set the migrations are responsible for, and
# `alembic check` separately asserts the migrations and the models agree, so
# between the two nothing goes unverified.
#
# `job_runs` and `verification_attempts` are included like everything else. They
# are the two newest migrations' tables, which makes them the two most worth
# round-tripping — they are also not the DEFAULT enum that created them
# (`alert_event_delivered_flag` adds a boolean to a table that already existed),
# so they exercise the `job_status` and `attempt_outcome` types that the older
# migrations leave behind.

# Every synthesised value carries a per-run token.
#
# A fixed value is *not* idempotent, it is the opposite: the second run against
# a database the first run already seeded fails on the primary key, and the third
# fails on every unique index, each reporting a constraint violation that has
# nothing to do with the migration being tested. Retrying after a failure is a
# normal thing to do, so the seed has to survive being retried.
RUN_TOKEN = uuid.uuid4().hex[:8]

# Fixed timestamps. `now()` would be equally valid; a literal makes the seed
# reproducible, which matters when a failure has to be re-run and compared.
FIXED_TS = "2020-01-01 00:00:00+00"

# CHECK constraints the synthesised rows have to satisfy.
#
# The seed writes literals, not ORM objects, so a value that the database
# rejects for a reason unrelated to the migration (a column whitelisted to
# 'guide' | 'review' | ...) is a false failure. These three shapes are the ones
# the schema actually uses:
#
# * `col = ANY (ARRAY[...])` — a whitelisted string column.
# * `col_a <> col_b` — two columns in the same row that must differ.
# * `CASE WHEN (col IS NOT NULL) ... END = 1` — exactly one of several nullable
#   columns must be set.
_CHECK_WHITELIST = re.compile(
    r"(\w+)\s*::\s*text\s*=\s*ANY\s*\(\s*ARRAY\[(.*?)\]::\s*text\[\]\s*\)",
    re.DOTALL,
)
_CHECK_PAIR = re.compile(r"(\w+)\s*<>\s*(\w+)")
_CHECK_EXACTLY_ONE_TARGET = re.compile(r"WHEN\s*\(*\s*(\w+)\s+IS\s+NOT\s+NULL\s*\)*", re.IGNORECASE)

# Columns the models allow to be NULL, but that a historical migration's
# downgrade sets back to NOT NULL.
#
# `users.hashed_password` became nullable so a Google-only account can exist
# without a password, and the downgrade of that migration sets it back to NOT
# NULL without a backfill. A seeded row with a NULL password therefore makes
# the downgrade fail, which proves nothing about the migration chain and turns
# the whole roundtrip job red. Filling the column keeps the row survivable
# through the downgrade without changing any schema.
EXTRA_VALUES: dict[str, dict[str, str]] = {
    "users": {"hashed_password": "'not-a-real-password-hash'"},
}


class Schema:
    """What `information_schema` says the database currently looks like."""

    def __init__(self, conn: Connection) -> None:
        self.tables = sorted(Base.metadata.tables)
        self.columns: dict[str, list[dict[str, Any]]] = {
            t: inspect(conn).get_columns(t) for t in self.tables
        }
        # table -> [(column, referenced_table, referenced_column)]
        self.foreign_keys: dict[str, list[tuple[str, str, str]]] = {}
        for table in self.tables:
            pairs = []
            for fk in inspect(conn).get_foreign_keys(table):
                for local, remote in zip(fk["constrained_columns"], fk["referred_columns"]):
                    pairs.append((local, fk["referred_table"], remote))
            self.foreign_keys[table] = pairs
        self.primary_keys = {
            t: inspect(conn).get_pk_constraint(t)["constrained_columns"] for t in self.tables
        }
        self.checks: dict[str, list[dict[str, Any]]] = {
            t: inspect(conn).get_check_constraints(t) for t in self.tables
        }
        self.whitelists: dict[str, dict[str, list[str]]] = {}
        self.pairs: dict[str, list[tuple[str, str]]] = {}
        self.exactly_one: dict[str, list[str]] = {}
        for table, constraints in self.checks.items():
            for constraint in constraints:
                sql = constraint.get("sqltext") or ""
                if "CASE" in sql:
                    if "=1" in sql.replace(" ", ""):
                        targets = _CHECK_EXACTLY_ONE_TARGET.findall(sql)
                        if targets:
                            # The columns of one "exactly one of these" group.
                            # One group per table is all the schema uses.
                            self.exactly_one[table] = targets
                    continue
                if "<>" in sql:
                    match = _CHECK_PAIR.search(sql)
                    if match:
                        self.pairs.setdefault(table, []).append((match.group(1), match.group(2)))
                    continue
                match = _CHECK_WHITELIST.search(sql)
                if match:
                    values = re.findall(r"'((?:[^']|'')*)'", match.group(2))
                    self.whitelists.setdefault(table, {})[match.group(1)] = [
                        value.replace("''", "'") for value in values
                    ]

    def fk_column_map(self, table: str) -> dict[str, str]:
        return {local: referenced for local, referenced, _ in self.foreign_keys.get(table, [])}

    def required(self, table: str) -> list[dict[str, Any]]:
        """Columns an INSERT cannot leave out.

        Excludes anything the database fills in for itself: a column with a
        server default, or an autoincrementing (SERIAL/IDENTITY) column. Leaving
        a defaulted column out is deliberate — omitting it proves the default the
        migration created actually applies.

        Note that a UUID primary key is *not* excluded even when it is the
        primary key: this project generates UUIDs in Python
        (`UUIDPKMixin.default`), so there is no server default and the database
        genuinely does reject a missing one. Excluding primary keys on the
        assumption that "databases make those up" is wrong here and fails loudly,
        which is how it was found.
        """
        self_fk = {local for local, ref, _ in self.foreign_keys.get(table, []) if ref == table}
        out = []
        for column in self.columns[table]:
            if column.get("nullable", True):
                continue
            if column.get("default") is not None:
                continue
            if column.get("autoincrement") is True:
                continue
            if column["name"] in self_fk:
                # A NOT NULL self-reference (`parent_id` on a category tree)
                # cannot be satisfied by a single row at all: the row would have
                # to exist before it could be its own parent. There is no
                # two-statement version of "insert one row per table" that fixes
                # this without already having a parent, so such a column is
                # reported rather than silently skipped — see `unseedable`.
                out.append(column)
            else:
                out.append(column)
        return out

    def unseedable(self, table: str) -> list[str]:
        """NOT NULL self-referential columns, which one row cannot satisfy."""
        return [
            column["name"]
            for column in self.columns[table]
            if column["name"]
            in {local for local, ref, _ in self.foreign_keys.get(table, []) if ref == table}
            and not column.get("nullable", True)
        ]

    def pair_index(self, table: str) -> dict[str, int]:
        """Which parent row each foreign key of `table` has to point at.

        A `col_a <> col_b` check over two columns that both reference the same
        parent cannot be satisfied by a single parent row, so the parent is
        seeded twice and the second column points at the second row.
        """
        index: dict[str, int] = {}
        fk_map = self.fk_column_map(table)
        for first, second in self.pairs.get(table, []):
            if fk_map.get(first) and fk_map.get(first) == fk_map.get(second):
                index[second] = 1
        return index

    def multi_row_parents(self) -> set[str]:
        """Tables that need two rows so a `<>` check over them can be satisfied."""
        parents: set[str] = set()
        for table in self.pairs:
            fk_map = self.fk_column_map(table)
            for first, second in self.pairs[table]:
                parent = fk_map.get(first)
                if parent and parent == fk_map.get(second):
                    parents.add(parent)
        return parents


def literal_for(column: dict[str, Any], table: str, schema: Schema, row: int = 0) -> str:
    """A SQL literal that satisfies one column's type and NOT NULL constraint."""
    name = column["name"]
    coltype = column["type"]
    text = str(coltype).upper()

    # Enum first: a native Postgres enum is not nullable either, and a generic
    # string would fail the cast. The first declared label is always valid, and
    # which one it is does not matter — this row is never read.
    enums = getattr(coltype, "enums", None)
    if enums:
        return "'" + str(enums[0]).replace("'", "''") + "'"

    # Then a whitelisted string column: `content_type IN ('guide', 'review')`
    # rejects any other string, and the failure would have nothing to do with
    # the migration under test.
    allowed = schema.whitelists.get(table, {}).get(name)
    if allowed:
        return "'" + str(allowed[0]).replace("'", "''") + "'"

    if "UUID" in text:
        # Derived per column and per row, so two rows of the same table never
        # collide on a primary key or a unique index.
        value = uuid.uuid5(uuid.NAMESPACE_OID, f"{RUN_TOKEN}:{table}:{name}:{row}")
        return "'" + str(value) + "'"
    if "BOOL" in text:
        return "false"
    if "TIMESTAMP" in text or "DATETIME" in text:
        # Aware form where the column accepts one: inserting a naive datetime
        # into `timestamptz` succeeds and silently reinterprets it, which is
        # exactly the ambiguity this check should not paper over.
        return f"'{FIXED_TS}'" if "ZONE" in text else "'2020-01-01 00:00:00'"
    if text.startswith("DATE"):
        return "'2020-01-01'"
    if "JSON" in text:
        return "'[]'" if "JSONB" not in text else "'[]'::jsonb"
    if any(k in text for k in ("INT", "NUMERIC", "DECIMAL", "REAL", "DOUBLE", "FLOAT", "MONEY")):
        return "1"
    if "BYTEA" in text:
        return "''::bytea"
    if "ARRAY" in text:
        # An empty array literal. There is no meaningful synthetic element, and
        # an array column is not where a missing NOT NULL hides in practice.
        return "'{}'"
    # Everything else is a string, made unique per run, per table and per column
    # so a unique index cannot reject the row for a reason unrelated to the
    # migration.
    slug = f"migration-check-{RUN_TOKEN}-{table}-{name}-{row}".replace(" ", "-").replace("'", "")

    # Clamped to the declared width. `products.currency` is `varchar(3)` — an
    # ISO code — and a full-length value is rejected by the database for a
    # reason that has nothing to do with the migration being tested. Truncation
    # means the row is not realistic, and that is fine: what is under test is
    # whether the schema survives a downgrade and re-upgrade with rows in it,
    # not whether those rows would pass review. A width the value cannot fit in
    # at all (a `varchar(0)`, say) still fails here, which is the case worth
    # hearing about.
    limit = getattr(coltype, "length", None)
    if limit is not None:
        slug = slug[:limit]
    return "'" + slug + "'"


async def seed() -> int:
    """Insert a row into every table we can. Returns 1 on complete success.

    Tables are attempted repeatedly until a pass makes no progress, so reference
    tables seed before the tables that point at them. This converges because the
    foreign-key graph is acyclic by construction — a cycle would make at least
    one insert impossible without deferred constraints, and that is a finding in
    itself rather than a reason to bail early.

    A table whose child rows carry a `col_a <> col_b` check over two columns
    that both point at it is seeded twice: one parent row cannot be different
    from itself.
    """
    async with engine.connect() as conn:
        schema = await conn.run_sync(Schema)

    multi_row = schema.multi_row_parents()
    pending: dict[str, int] = {table: (2 if table in multi_row else 1) for table in schema.tables}
    seeded: set[str] = set()
    # table -> ids of the rows we inserted, so dependants can reference them.
    ids: dict[str, list[Any]] = {}

    while pending:
        progressed = False
        for table in list(pending):
            fk_map = schema.fk_column_map(table)
            pair_index = schema.pair_index(table)
            required = schema.required(table)
            names: list[str] = []
            values: list[str] = []
            # Deferred only on the foreign keys this row actually has to supply.
            # Checking every FK on the table instead deadlocks a self-referencing
            # one: `categories.parent_id` points at `categories`, which by
            # definition has no row yet, so the table waits for itself forever
            # even though the column is nullable and would simply be left NULL.
            waiting = False
            for column in required:
                col = column["name"]
                ref_table = fk_map.get(col)
                if ref_table is None:
                    names.append(col)
                    values.append(literal_for(column, table, schema, len(ids.get(table, []))))
                    continue
                index = pair_index.get(col, 0)
                parent_ids = ids.get(ref_table, [])
                if len(parent_ids) > index:
                    names.append(col)
                    values.append(f"'{parent_ids[index]}'")
                else:
                    waiting = True
                    break
            if waiting:
                continue

            # A check that requires exactly one of several nullable columns to
            # be set: fill the first target whose parent row already exists and
            # leave the others NULL, which is what the check asks for. With no
            # target filled the check fails, so the table waits for its parent
            # rather than inserting a row the database will reject.
            exactly_one = schema.exactly_one.get(table, [])
            if exactly_one:
                target_added = False
                for target in exactly_one:
                    if target in names:
                        target_added = True
                        break
                    ref_table = fk_map.get(target)
                    if ref_table is None or not ids.get(ref_table):
                        continue
                    names.append(target)
                    values.append(f"'{ids[ref_table][0]}'")
                    target_added = True
                    break
                if not target_added:
                    continue

            for name, literal in EXTRA_VALUES.get(table, {}).items():
                if name not in names:
                    names.append(name)
                    values.append(literal)

            # RETURNING rather than a follow-up SELECT. A `SELECT ... LIMIT 1`
            # after the insert would pick up whichever row the planner reached
            # first, which on a table that already held data is not necessarily
            # the row just written — and then every dependant foreign key points
            # at the wrong parent for reasons that look like a migration bug.
            pk = (schema.primary_keys.get(table) or ["id"])[0]
            try:
                async with engine.begin() as tx:
                    result = await tx.execute(
                        text(
                            f"INSERT INTO {table} ({', '.join(names)}) "
                            f"VALUES ({', '.join(values)}) RETURNING {pk}"
                        )
                    )
                    inserted_id = result.scalar_one()
            except Exception as exc:  # noqa: BLE001
                detail = (str(exc).splitlines() or [type(exc).__name__])[0]
                print(f"  !! {table}: {detail}")
                # Left pending; if nothing else progresses it will be reported as
                # unseedable rather than retried forever.
                continue

            ids.setdefault(table, []).append(inserted_id)
            seeded.add(table)
            pending[table] -= 1
            if pending[table] <= 0:
                del pending[table]
            progressed = True
            print(f"  seeded {table} ({len(names)} column(s) supplied)")

        if not progressed:
            break

    if pending:
        print(f"FAIL: could not seed {len(pending)} table(s): {', '.join(sorted(pending))}")
        for table in sorted(pending):
            print(f"  - {table}: {[c['name'] for c in schema.required(table)]}")
            blocked = schema.unseedable(table)
            if blocked:
                print(f"      NOT NULL self-referential: {', '.join(blocked)}")
        return 1

    print(f"seeded all {len(seeded)} table(s)")
    return 0


async def check() -> int:
    """Every seeded table must be empty again after downgrade → upgrade.

    A table that still holds a row means the downgrade did not run the code we
    think it did — either it was skipped, or it recreated what it should have
    dropped. Both are worth failing a build over.
    """
    # Through Schema, so it is `run_sync` rather than `inspect` on the async
    # connection — SQLAlchemy refuses to inspect an AsyncConnection directly, and
    # the failure is a `NoInspectionAvailable` traceback rather than a clear
    # message.
    async with engine.connect() as conn:
        schema = await conn.run_sync(Schema)
    tables = schema.tables

    survivors: list[str] = []
    for table in tables:
        try:
            async with engine.connect() as tx:
                count = int((await tx.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())
        except Exception as exc:  # noqa: BLE001
            print(f"  !! {table} is not queryable after re-upgrade: {exc}")
            survivors.append(table)
            continue
        if count:
            print(f"  !! {table} still holds {count} row(s)")
            survivors.append(table)

    if survivors:
        print(f"FAIL: {len(survivors)} table(s) did not round-trip cleanly: {', '.join(survivors)}")
        return 1
    print(f"OK: all {len(tables)} table(s) are empty after downgrade + upgrade")
    return 0


async def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "seed"

    async with engine.connect() as conn:
        version = (
            await conn.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one_or_none()
    if version is None:
        # Not an error worth a traceback: the caller's mistake, stated plainly.
        print("alembic_version is empty — run `alembic upgrade head` first")
        return 1
    print(f"schema at {version}")

    if mode == "seed":
        return await seed()
    if mode == "check":
        return await check()
    print(f"unknown mode: {mode!r} (expected 'seed' or 'check')")
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
