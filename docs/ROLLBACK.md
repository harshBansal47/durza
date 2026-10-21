# Rollback runbook — deploy host

The commands below run **on the deploy host**, in `~/durza`. They use the same
compose file as the deploy step (`podman-compose`, no `-f`), so the rollback
starts the same stack the deploy started.

## What the deploy actually runs, and what that means

The deploy step is:

```bash
ssh "$VM_USER@$VM_HOST" "cd ~/durza \
  && git pull origin main \
  && podman-compose build app \
  && podman-compose run --rm --no-deps -T app alembic upgrade head \
  && podman-compose up -d app"
```

`podman-compose` with no `-f` picks up **`docker-compose.yml`**, which is the
*development* compose file, not `docker-compose.prod.yml`:

| | `docker-compose.yml` (in use) | `docker-compose.prod.yml` (intended) |
|---|---|---|
| app command | `uvicorn … --reload` | `uvicorn …` (no reload) |
| published port | `8000:8000` (all interfaces) | `127.0.0.1:8000:8000` |
| local services | starts `db` (Postgres) and `redis` | none — Neon and Upstash |
| restart policy | none | `restart: unless-stopped` |
| source | bind-mounted `./app`, `./alembic` | same bind mounts |

Two consequences matter for a rollback:

1. **The running code is the checkout, not the image.** `./app` and `./alembic`
   are bind-mounted, and `uvicorn --reload` restarts the app when those files
   change. So `git pull origin main` alone puts new code in front of users,
   even if the migration and `podman-compose up` never run. That is exactly
   what happened on 2026-10-21: the pull at 12:26 UTC replaced the code, the
   migration was refused by the production guard at 12:31 UTC, and the app kept
   serving the new code against the old schema.
2. **The image tag moves.** `podman-compose build app` re-tags
   `localhost/durza_app:latest` to the new image. The old image loses that tag
   and becomes dangling — and `podman image prune -f` deletes dangling images.
   The old image must be tagged *before* the build (below), or it is gone.

## Before a deploy: tag what is running

```bash
cd ~/durza
CONTAINER=$(podman-compose ps -q app)
IMG_ID=$(podman inspect --format '{{.Image}}' "$CONTAINER")
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
podman tag "$IMG_ID" "localhost/durza_app:pre-phase-5-$STAMP"
git rev-parse HEAD | tee "/tmp/durza-deployed-sha-$STAMP.txt"
podman images --format '{{.Repository}}:{{.Tag}}\t{{.Id}}\t{{.CreatedSince}}'
```

Tag the **image id**, not `latest`: `latest` is about to be re-pointed at the
new build. A tagged image is not removed by `podman image prune -f`.

## The half-deployed state, and what to check

The 2026-10-21 deploy left the host in this state, and it is the state to
expect if the migration step is refused:

- the checkout is on the new `main` (so the bind mount serves the new code);
- the schema is still at the pre-migration revision (the guard refuses before
  it opens a connection, so no DDL ran);
- the container still runs the old image, and the app is serving the new code
  from the bind mount;
- `podman-compose up -d app` never ran, so the container was not recreated.

Check it with:

```bash
cd ~/durza
git rev-parse HEAD
podman-compose ps
podman inspect --format '{{.Image}}  {{.Config.Image}}' "$(podman-compose ps -q app)"
curl -s http://127.0.0.1:8000/health
```

The app tolerates that state as long as the migration is additive, which this
one is: the reconciliation only adds foreign keys, `DEFAULT now()` columns and
indexes. What it does *not* tolerate is writing to the eight tables that have
no `created_at`/`updated_at` default yet (`coupon_staging`,
`daily_content_stats`, `sale_series`, `sale_editions`,
`sale_edition_coupon_rules`, `sale_slug_history`, `user_interests`,
`user_segments`) — the model's `server_default` makes the database fill those
columns, and without the database default the insert fails. Today nothing
writes to them: `coupon_staging` has no insert anywhere in the app, the
personalization endpoints are 404 while the flags are off, and the content
rollup only runs inside the scheduler, which is off.

## Roll the code back to the last good SHA

```bash
cd ~/durza
git fetch origin
git checkout --detach a43df9ce
podman-compose build app
podman-compose up -d app
curl -s http://127.0.0.1:8000/health
git checkout main
```

The last line returns the working tree to `main`, so the next deploy starts
from a clean checkout instead of a detached HEAD.

**There is no `alembic` command in the rollback, on purpose.** The
reconciliation migration (`b1c2d3e4f5a6`) is additive: it adds two foreign
keys, sixteen `DEFAULT now()` columns and four indexes, and drops nothing. The
old code therefore runs correctly against the migrated schema. `a43df9ce` also
cannot locate `5ee40ae51cc1` in its alembic tree, so `alembic upgrade head`
from that checkout would fail.

If you want the old image without rebuilding it, point `latest` at the tagged
one first:

```bash
podman tag "localhost/durza_app:pre-phase-5-<STAMP>" localhost/durza_app:latest
podman-compose up -d app --no-build
```

## Reverting the schema as well (only if you really need to)

Only from the **merged** tree, because that is the only tree that knows the
revision `5ee40ae51cc1`:

```bash
cd ~/durza
git checkout main
podman-compose run --rm --no-deps -T app alembic downgrade 5ee40ae51cc1
```

It removes exactly what `b1c2d3e4f5a6` added and nothing else. Take a restore
point first. After this the schema is back at the pre-reconciliation state,
where eight tables have no `created_at`/`updated_at` default and
`coupon_staging` has no foreign keys.

## After any rollback

```bash
cd ~/durza
podman-compose ps
podman logs --tail 50 "$(podman-compose ps -q app)"
bash scripts/post_deploy_verify.sh
```

`scripts/post_deploy_verify.sh` is read-only: it checks the schema version,
`/health`, `/health/deep`, the coupon list, the personalization endpoints that
must answer 404 while the flags are off, the CORS preflight and the consent
checks from `docs/ANALYTICS_VERIFY.md`, and the analytics row counts. It writes
nothing. `docs/POST_DEPLOY.md` is the full post-deploy checklist.
