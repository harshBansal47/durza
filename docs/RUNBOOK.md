# Operator runbook — Durza backend

This is the runbook for the API as it is deployed today. It matches what is
actually running; where a procedure changed recently, the change is called out.

## How a deploy happens

Every push to `main` runs the `ci` workflow:

1. `backend` — ruff, format, mypy, pytest, the destructive-migration guard, a
   fresh `alembic upgrade head` and `alembic check`.
2. `migrations down then up` — the whole chain is downgraded to base and back
   with data in it, which proves the chain is reversible.
3. `deploy` — only on a push to `main`, and only when the push can change what
   runs.

The deploy step, in order:

```bash
cd ~/durza
git pull origin main
podman-compose build app
podman-compose run --rm --no-deps -T -e ALLOW_PROD_MIGRATION=1 app alembic upgrade head
podman-compose up -d app
podman image prune -f
```

- The **migration runs before the new container starts**, in a throwaway
  container built from the new image. If the migration fails, the old
  container keeps serving the old code against the old schema, and the deploy
  fails loudly.
- `ALLOW_PROD_MIGRATION=1` is passed **per run**, on that one container. It is
  never written into the VM's `.env`.
- Deploys **queue** behind each other and are never cancelled: the deploy job
  has its own concurrency group with `cancel-in-progress: false`, and the
  workflow-level group only cancels superseded runs on pull requests, where
  nothing is deployed.

### Which pushes deploy

The deploy job compares the push against the **last SHA that was
successfully deployed** (recorded on the host at the end of a successful
deploy), not against the previous head of `main`. So:

- a documentation-only push, with the last deploy green → **skipped**;
- a documentation-only push, after a deploy **failed** → **deployed**, because
  the failed deploy's changes are still missing;
- no recorded SHA (the first deploy after this feature landed, or a host that
  never recorded one) → **deployed**.

The paths that trigger a deploy: `app/`, `alembic/`, `mcp_server/`,
`scripts/`, `Dockerfile`, `docker-compose.yml`, `docker-compose.prod.yml`,
`pyproject.toml`, `uv.lock`, `.github/workflows/ci.yml`. Anything outside that
list is skipped — so a file left out of the list merges **without** deploying.

## The migration guard

`alembic/env.py` refuses to run against production:

| command | in production |
|---|---|
| `downgrade` | **always refused**, no override exists |
| `stamp` | **always refused**, no override exists |
| `upgrade` | allowed only with `ALLOW_PROD_MIGRATION=1` in that run's environment |
| `current`, `check`, `history`, `heads`, `branches`, `version` | allowed (read-only) |

The check happens before the engine is created, so a refused command opens no
connection at all. A production schema is restored from a restore point, not by
a command.

Outside production nothing is restricted.

## Rolling back

```bash
cd ~/durza

# tag what is running before you change anything
CONTAINER=$(podman-compose ps -q app)
IMG_ID=$(podman inspect --format '{{.Image}}' "$CONTAINER")
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
podman tag "$IMG_ID" "localhost/durza_app:pre-rollback-$STAMP"
git rev-parse HEAD | tee "/tmp/durza-deployed-sha-$STAMP.txt"

# go back to the last good code
git fetch origin
git checkout --detach a43df9ce      # the last known good deploy
podman-compose build app
podman-compose up -d app
curl -s http://127.0.0.1:8000/health

git checkout main
```

`podman-compose build` re-tags `latest`, so the old image has to be tagged
**by id** before the build or it is lost. The reconciliation migration is
additive, so the old code runs correctly against the migrated schema and the
rollback does not touch the database. If the schema itself has to go back,
that is a separate, deliberate step from the merged tree:
`podman-compose run --rm --no-deps -T -e ALLOW_PROD_MIGRATION=1 app alembic downgrade 5ee40ae51cc1`
— take a restore point first.

More detail in `docs/ROLLBACK.md`; the after-deploy checklist is in
`docs/POST_DEPLOY.md`.

## Verifying a deploy

```bash
bash scripts/post_deploy_verify.sh
```

Read-only: the schema version, `/health`, `/health/deep`, the coupon list, the
personalization endpoints that must answer 404 while the flags are off, the
CORS preflight and consent checks, and the analytics row counts. The
analytics-verification runbook is `docs/ANALYTICS_VERIFY.md`.

## Backups

The **production database is backed up by Neon**, not by this repository. The
ops database (a local Postgres with the `durza_ops` schema) is a different
thing and has its own backup command — see `durza-ops/docs/BACKUP.md`.

## Creating and publishing a sale hub

A sale hub — "Diwali 2026" — is a series, one or more dated editions, and the
rules that decide which coupons the edition shows.

1. **The guide first.** The hub points at a piece of editorial content, and
   that content has to be published before the hub can be published. A draft
   guide means a refused publish, by design.
2. **Create the series** in the back office (`/admin` → *Sale series*): the
   slug (`diwali-2026`, part of the URL, so pick it once), the title and a
   one-line description.
3. **Create the edition**: the dates, the year, the source the dates come
   from, and the published guide it points at.
4. **Add the rules**: one per store slug (`croma`) or category slug. A rule
   naming a store or category that does not exist is ignored; a `tag` rule
   resolves to nothing, because a coupon has no tag. Up to 20 per edition.
5. **Publish**: select the edition → *Publish*. The service refuses an
   edition whose guide is not published or whose confirmed dates have no
   source.
6. **The lifecycle** (`announced` → `upcoming` → `live` → `ended` →
   `archived`) follows the dates only when the scheduler is on, so for a hub
   that has to be live now, set `lifecycle_status` to `live` yourself. A
   published `announced` edition is still shown as the current one.
7. **The storefront** answers 404 on every `/sales` route while
   `NEXT_PUBLIC_SALE_HUBS_ENABLED` is off — turning it on is a web deploy,
   not a content change.

The full runbook, with the exact fields, is `docs/SALE_HUBS.md` (added by
PR #14). Check the result with:

```bash
curl -s https://api.durza.in/api/v1/sales/diwali-2026 | head -c 400
```

## The kill switch

`durza_ops.governance.killswitch` is a file (`OPS_KILLSWITCH_FILE`) that turns
the ops tooling off without a deploy. It is checked on every tool call. The
publishing caps, the review queue, the budget ledger, the audit sampling and
the YMYL guard all apply to anything that runs unattended.
