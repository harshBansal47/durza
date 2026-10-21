# Rollback runbook — deploy host

The commands below run **on the deploy host**, in `~/durza`. They use the same
compose file as the deploy step (`podman-compose` with no `-f`), so the rollback
starts the same stack the deploy started.

## Before a deploy: tag what is running

```bash
cd ~/durza
CONTAINER=$(podman-compose ps -q app)
IMG_ID=$(podman inspect --format '{{.Image}}' "$CONTAINER")
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
podman tag "$IMG_ID" "localhost/durza-app:pre-phase-5-$STAMP"
git rev-parse HEAD | tee "/tmp/durza-deployed-sha-$STAMP.txt"
podman images --format '{{.Repository}}:{{.Tag}}\t{{.Id}}\t{{.CreatedSince}}'
```

`podman inspect --format '{{.Image}}'` returns the image ID, so the tag points at
the exact image that was running, whatever its name was. The deploy step does
**not** run `podman image prune -f` for the Phase 5 deploy, so the previous
image stays on the host and this tag survives.

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

The last line returns the working tree to `main` so the next deploy starts from
a clean checkout instead of a detached HEAD.

**There is no `alembic` command in the rollback, on purpose.** The reconciliation
migration (`b1c2d3e4f5a6`) is additive: it adds two foreign keys, sixteen
`DEFAULT now()` columns and four indexes, and drops nothing. The old code
therefore runs correctly against the migrated schema. `a43df9ce` also cannot
locate `5ee40ae51cc1` in its alembic tree, so `alembic upgrade head` from that
checkout would fail.

## Reverting the schema as well (only if you really need to)

This is not part of a normal rollback, and it must be run from the **merged**
tree, because that is the only tree that knows the revision `5ee40ae51cc1`:

```bash
cd ~/durza
git checkout main
podman-compose run --rm --no-deps -T app alembic downgrade 5ee40ae51cc1
```

It removes exactly what `b1c2d3e4f5a6` added and nothing else. Take a restore
point first, and remember that after this the schema is back at the
pre-reconciliation state, where eight tables have no `created_at`/`updated_at`
default and `coupon_staging` has no foreign keys.

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
nothing.
