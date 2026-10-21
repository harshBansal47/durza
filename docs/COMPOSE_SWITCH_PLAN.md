# Plan: switch the deploy host to the production compose file

**Nothing in this document has been applied.** It is the plan for a separate
pull request, to be made after the Phase 5 deploy is confirmed.

## The problem

The deploy step runs `podman-compose` with no `-f`, so it uses
`docker-compose.yml` — the **development** compose file. The production file,
`docker-compose.prod.yml`, exists and is not used.

| | `docker-compose.yml` (in use) | `docker-compose.prod.yml` (intended) |
|---|---|---|
| app command | `uvicorn … --reload` | `uvicorn …` (no reload) |
| published port | `8000:8000` (all interfaces) | `127.0.0.1:8000:8000` (loopback only) |
| local services | starts `db` (Postgres 16) and `redis` | none — Neon and Upstash |
| restart policy | none | `restart: unless-stopped` |
| source | bind-mounted `./app`, `./alembic` | same bind mounts |

What that costs us today:

1. **A `git pull` changes production code by itself.** The source is
   bind-mounted and `uvicorn --reload` restarts the app when those files
   change, so a deploy that fails after the pull still puts new code in front
   of users. That is exactly the half-deployed state of 2026-10-21 12:31 UTC.
2. **Port 8000 is published on all interfaces**, not just loopback.
3. **A local Postgres and Redis are started** and used by nothing: the app
   talks to Neon.
4. **No restart policy**: if the app crashes, it stays down until somebody
   runs `podman-compose up`.
5. Stray edits on the host are a production restart.

## Note: every merge to `main` deploys

The `deploy` job runs on **every** push to `main`, and the workflow has no
path filter. A merge that only touches documentation — this plan, a runbook, a
comment in a doc file — therefore rebuilds the image, runs the migration
container and recreates the app container on the deploy host.

That is not free: the build takes minutes, the migration container starts
against production, and the container is replaced. A docs-only merge should
not do any of that, and it does not need to.

The fix is to make the deploy job skip itself when nothing that can change
the running artefact changed. Note that **job-level `paths:` is not a
GitHub Actions key** — `paths` only exists on a workflow trigger, and
putting it there would filter the *whole* workflow, so a docs-only push would
stop running the tests as well. The deploy job therefore checks out the push,
diffs it against the previous `main`, and skips its own steps:

```yaml
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Detect whether this push can change what runs
        id: changes
        run: |
          set -euo pipefail
          BEFORE="${{ github.event.before }}"
          CHANGED="$(git diff --name-only "$BEFORE" "${{ github.sha }}")"
          DEPLOY=no
          while read -r file; do
            case "$file" in
              app/*|alembic/*|mcp_server/*|scripts/*|Dockerfile|docker-compose.yml|docker-compose.prod.yml|pyproject.toml|uv.lock|.github/workflows/ci.yml)
                DEPLOY=yes; break ;;
            esac
          done <<< "$CHANGED"
          echo "deploy=$DEPLOY" >> "$GITHUB_OUTPUT"

      - name: Set up SSH
        if: steps.changes.outputs.deploy == 'yes'
        ...

      - name: Pull, build, migrate, then start
        if: steps.changes.outputs.deploy == 'yes'
        ...
```

The `backend` and `migrations down then up` jobs still run on every push, so
the tests do not stop being the gate — only the deploy does. The list has to
be complete: a file left out merges **without** deploying.

This is a **separate small pull request** from the compose switch, because it
is a different change with a different risk: the filter has to be complete, or
a change that matters will be merged without deploying. Proposed as its own
PR so it can be reviewed on its own merits.

## Before the change: prove what is reachable

Record the "before" state. Every one of these is read-only.

**From outside the host (any other machine):**

```bash
# 1. the API answers through the proxy
curl -sS -o /dev/null -w 'api.durza.in/health -> %{http_code}\n' https://api.durza.in/health

# 2. is the app's port reachable directly from the internet today?
#    (the production compose file binds it to loopback, so this is expected
#    to go from "open" to "closed" — which is the point, but record it)
nc -vz <vm-host> 8000
```

**On the host:**

```bash
cd ~/durza

# 3. the app answers locally
curl -sS -o /dev/null -w '127.0.0.1:8000/health -> %{http_code}\n' http://127.0.0.1:8000/health

# 4. what is listening on 8000, and on which address
ss -ltnp | grep ':8000'

# 5. what does Caddy actually proxy to
systemctl is-active caddy
grep -nE 'reverse_proxy|:8000|api\.durza\.in' /etc/caddy/Caddyfile
curl -sS -o /dev/null -w 'direct-to-upstream -> %{http_code}\n' \
  -H 'Host: api.durza.in' http://127.0.0.1:8000/health

# 6. which compose file the deploy actually used
podman inspect --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' \
  "$(podman-compose ps -q app)"
```

If step 5 shows the upstream is the host's **public address** rather than
`127.0.0.1:8000`, the prod compose file (loopback-only) would break the proxy:
fix Caddy first, or do not switch. If step 2 shows 8000 is reachable from the
internet today, switching closes it — which is the intent, but say so out loud
before doing it.

## The change

In the deploy step, use the production compose file for all three commands:

```diff
-  && podman-compose build app \
-  && podman-compose run --rm --no-deps -T -e ALLOW_PROD_MIGRATION=1 app alembic upgrade head \
-  && podman-compose up -d app
+  && podman-compose -f docker-compose.prod.yml build app \
+  && podman-compose -f docker-compose.prod.yml run --rm --no-deps -T -e ALLOW_PROD_MIGRATION=1 app alembic upgrade head \
+  && podman-compose -f docker-compose.prod.yml up -d app
```

and the same in `docs/ROLLBACK.md`.

**Do not stop the local `db` and `redis` containers.** `durza-ops` points at
`127.0.0.1` and its `durza_ops` schema lives in that local database, so
`podman-compose -f docker-compose.yml down` would take the ops data with it.
Switching only means the *app* is no longer started from the development
compose file; the containers that are already running stay running.

## After the change: prove nothing broke

Repeat checks 1–5 from above and compare:

```bash
# from outside
curl -sS -o /dev/null -w 'api.durza.in/health -> %{http_code}\n' https://api.durza.in/health
nc -vz <vm-host> 8000          # expected to fail now: the port is loopback-only

# on the host
curl -sS -o /dev/null -w '127.0.0.1:8000/health -> %{http_code}\n' http://127.0.0.1:8000/health
ss -ltnp | grep ':8000'         # expect 127.0.0.1:8000, not 0.0.0.0:8000
podman-compose -f docker-compose.prod.yml ps
```

Then the read-only verification:

```bash
bash scripts/post_deploy_verify.sh
```

## What changes for day-to-day work

- **No hot reload.** The source is still bind-mounted, but without `--reload`
  a code change on the host does nothing until the container is recreated. The
  deploy's `up -d app` does that. A stray edit on the host is no longer a
  production restart.
- **The app comes back after a crash** (`restart: unless-stopped`).
- **A rollback has to use the same file**: `podman-compose -f
  docker-compose.prod.yml up -d app`.

## Rolling it back

```bash
cd ~/durza
git revert <the commit that switched the compose file>
```

or, by hand, change the deploy step back to the bare `podman-compose` and
re-run the deploy job. The schema is untouched by this change — it is a
process change, not a data one.
