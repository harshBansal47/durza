# Migration Safety Guide

## The Guard

`scripts/check_destructive_migrations.py` is a CI gate that fails any pull
request or push to `main` when a migration's `upgrade()` function contains
`op.drop_table` or `op.drop_column`.

**Why:** A previous migration misread a new model as a rename and dropped the
`conversions` table. Once that table holds real revenue data, the same mistake
would be unrecoverable. The guard makes destructive operations a deliberate,
reviewed decision rather than an accident.

## Running Locally

```bash
# From the durza/ directory
uv run python scripts/check_destructive_migrations.py
```

Expected output on a clean tree:

```
OK: no drop_table/drop_column in any migration upgrade() body.
```

## Failure Modes

### A migration drops a table or column

```
Destructive operations found in migration upgrade() bodies:
  - abc123_add_foo.py:42 upgrade() calls op.drop_table(...)

If this drop is intentional, re-run with OVERRIDE_DROP=1 and a reviewed
deployment plan (and a pg_dump first).
```

**Resolution:** If the drop is genuinely required (e.g. removing a column
that is no longer used), you must:

1. Take a `pg_dump` backup of the production database.
2. Set `OVERRIDE_DROP=1` in the CI environment (or run locally with
   `OVERRIDE_DROP=1 uv run python scripts/check_destructive_migrations.py`).
3. Add a comment to the migration explaining why the drop is safe.
4. Get explicit review approval for the destructive change.

### The allow-list

One historical migration (`7b3f1a9c4d21_multi_source_core.py`) contains a
`drop_column` in its `upgrade()` that removed `ingestion_runs.skipped`, a
column no code path ever wrote. This is documented in the `_ALLOWLIST` in
the guard script. New drops do not join this list without a reviewed
incident write-up.

## CI Integration

The guard runs in two places:

1. **GitHub Actions** (`.github/workflows/ci.yml`): Fails the backend CI job
   on any PR or push to `main`.
2. **Pre-commit** (`.pre-commit-config.yaml`): Runs automatically on every
   `git commit` via the `destructive-migration-guard` local hook.

## Override Flag

Set `OVERRIDE_DROP=1` (or `true` / `yes`) in the environment to bypass the
guard for a single run. This is intended for reviewed, intentional
destructive migrations only. The override is logged in CI output so it is
never silent.
