# Post-deploy checklist — Phase 5

Run this after the deploy job for `main` has gone green. Everything here is
read-only except the two steps that are explicitly marked as changing
something.

## 1. The deploy job log

Open the run for the merge commit and read the `deploy` job, step
*Pull, build, migrate, then start*. The lines that matter, in order:

```
  a43df9c..b46ced0  main       -> origin/main          # the checkout moved
Successfully tagged localhost/durza_app:latest          # the new image
... podman run ... durza_app alembic upgrade head
INFO  [alembic.runtime.migration] Running upgrade 5ee40ae51cc1 -> b1c2d3e4f5a6, reconcile schema drift after phase 5
exit code: 0
... podman-compose up -d app
```

The one line to look for is:

```
Running upgrade 5ee40ae51cc1 -> b1c2d3e4f5a6, reconcile schema drift after phase 5
```

and the step ending in `Process completed with exit code 0`. If the step instead
ends with

```
RuntimeError: Refusing to run Alembic against production. Set ALLOW_PROD_MIGRATION=1 only for an approved production migration.
```

then **no DDL ran**: the guard refuses before it opens a connection, so the
schema is untouched and the app keeps serving whatever the bind mount gives it.
That is the failure mode of 2026-10-21 12:31 UTC (see `docs/ROLLBACK.md`).

## 2. Neon (read-only)

```sql
-- 1. the version
select version_num from alembic_version;
-- expect: b1c2d3e4f5a6

-- 2. the two foreign keys on coupon_staging, both validated
select conname, conrelid::regclass::text, confdeltype, convalidated
from pg_constraint
where conname in ('fk_coupon_staging_source_id', 'fk_coupon_staging_store_id');
-- expect 2 rows, convalidated = t, confdeltype 'c' (cascade) and 'n' (set null)

-- 3. the four indexes
select indexname from pg_indexes
where schemaname = 'public'
  and indexname in (
    'ix_coupons_quality_score_v2',
    'ix_sale_series_publication_status',
    'ix_sale_editions_publication_status',
    'ix_sale_editions_editorial_content_id');
-- expect 4 rows

-- 4. the sixteen created_at/updated_at defaults
select table_name, column_name, column_default
from information_schema.columns
where table_schema = 'public'
  and column_name in ('created_at', 'updated_at')
  and table_name in (
    'coupon_staging', 'daily_content_stats', 'sale_series', 'sale_editions',
    'sale_edition_coupon_rules', 'sale_slug_history', 'user_interests',
    'user_segments')
order by table_name, column_name;
-- expect 16 rows, every column_default = 'now()'

-- 5. nothing was dropped
select count(*) from click_events;      -- expect the same count as before
select count(*) from coupons;
```

## 3. Remove `ALLOW_PROD_MIGRATION`

The flag exists only for the one approved migration. Take it back out:

```bash
cd ~/durza
sed -i '/^ALLOW_PROD_MIGRATION=/d' .env
grep -c ALLOW_PROD_MIGRATION .env        # expect 0
```

The running container still carries the variable in its environment until it is
recreated, which is harmless (the app never reads it) but worth knowing:

```bash
podman exec "$(podman-compose ps -q app)" env | grep ALLOW_PROD_MIGRATION
```

Recreate the app container to drop it from the process environment, if you
want it gone immediately:

```bash
podman-compose up -d app
curl -s http://127.0.0.1:8000/health
```

## 4. Confirm which image and container are running

```bash
cd ~/durza
podman-compose ps                                   # container, image, status
CONTAINER=$(podman-compose ps -q app)
podman inspect --format '{{.Image}}  {{.Config.Image}}' "$CONTAINER"
podman images --format '{{.Repository}}:{{.Tag}}\t{{.Id}}\t{{.CreatedSince}}' | head
git rev-parse HEAD                                  # the source the bind mount serves
curl -s http://127.0.0.1:8000/health
```

The compose project is `durza` and the service is `app`, so the image is
`localhost/durza_app:latest` and the container is `durza_app_1`. The
bind-mounted `./app` means the **code** the container serves is the checkout's
working tree, not the image — which is why `git pull` alone changed the running
code on 2026-10-21 even though `podman-compose up` never ran.

## 5. The read-only verification script

```bash
cd ~/durza
bash scripts/post_deploy_verify.sh
```

It checks the schema version, `/health`, `/health/deep`, the coupon list, the
personalization endpoints that must answer 404 while the flags are off, the
CORS preflight and the consent checks from `docs/ANALYTICS_VERIFY.md`, and the
analytics row counts. It writes nothing: no `alembic` command, no accepted
event, only GETs, one OPTIONS, one cookie-clearing consent call and SELECT
counts.

Its schema-version line comes from the container, so it is only meaningful
when it runs **on the deploy host**. A run from elsewhere proves the API
answers, not that the production database is the one being read.
