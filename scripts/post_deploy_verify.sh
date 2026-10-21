#!/usr/bin/env bash
# Post-deploy verification — strictly READ ONLY.
#
# Nothing in this script writes to the database. It issues GETs and one
# OPTIONS preflight, one POST to /api/v1/analytics/consent (which clears
# cookies and writes nothing), and SELECT-only counts run through the app
# container.
#
# It deliberately does NOT run `alembic current`: alembic/env.py refuses to
# run at all when ENVIRONMENT=production unless ALLOW_PROD_MIGRATION=1, and
# that bypass exists for migrations, not for reading a version number. The
# schema version is read with a SELECT instead, which needs no bypass.
#
# Usage (on the deploy host):
#   bash scripts/post_deploy_verify.sh                       # http://127.0.0.1:8000
#   BASE_URL=https://api.durza.in bash scripts/post_deploy_verify.sh
#
# Environment:
#   BASE_URL              default http://127.0.0.1:8000
#   COMPOSE               default "podman-compose"
#   COMPOSE_FILE          default "" (empty = the same default compose file the
#                         deploy step uses, so the checks hit the same stack)
#   EXPECTED_REVISION     default b1c2d3e4f5a6
#   EXPECTED_ORIGIN       default https://www.durza.in (the origin
#                         docs/ANALYTICS_VERIFY.md uses)
#   SKIP_CONTAINER_CHECKS set to 1 to skip the checks that need the container
#                         (schema version, SQL counts)
#
# Exit code 0 only when every check passes.

set -uo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
COMPOSE="${COMPOSE:-podman-compose}"
COMPOSE_FILE="${COMPOSE_FILE:-}"
EXPECTED_REVISION="${EXPECTED_REVISION:-b1c2d3e4f5a6}"
EXPECTED_ORIGIN="${EXPECTED_ORIGIN:-https://www.durza.in}"
SKIP_CONTAINER_CHECKS="${SKIP_CONTAINER_CHECKS:-0}"

if [ -n "$COMPOSE_FILE" ]; then
  COMPOSE_CMD="$COMPOSE -f $COMPOSE_FILE"
else
  COMPOSE_CMD="$COMPOSE"
fi

PASS=0
FAIL=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

say()  { printf '%s\n' "$*"; }
ok()   { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$*"; }
bad()  { FAIL=$((FAIL + 1)); printf 'FAIL  %s\n' "$*"; }
note() { printf 'NOTE  %s\n' "$*"; }

# code URL -> http status
code() { curl -sS -o "$TMP/body" -w '%{http_code}' --max-time 20 "$@" 2>"$TMP/err"; }

say "Post-deploy verification (read only) — $BASE_URL"
say "compose: $COMPOSE_CMD"
say ""

# ---------------------------------------------------------------- 1. schema
if [ "$SKIP_CONTAINER_CHECKS" = "1" ]; then
  note "schema version — skipped (SKIP_CONTAINER_CHECKS=1)"
elif ! command -v $COMPOSE >/dev/null 2>&1; then
  note "schema version — skipped (no '$COMPOSE' on PATH)"
else
  VERSION="$($COMPOSE_CMD exec -T app python - <<'PY' 2>/dev/null
import asyncio
from sqlalchemy import text
from app.core.database import AsyncSessionLocal

async def main():
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text("select version_num from alembic_version"))).scalars().all()
        print(",".join(rows))

asyncio.run(main())
PY
)"
  if [ -z "$VERSION" ]; then
    bad "schema version — could not read alembic_version through the container"
  elif [ "$VERSION" = "$EXPECTED_REVISION" ]; then
    ok "schema version is $VERSION"
  else
    bad "schema version is '${VERSION:-<empty>}', expected '$EXPECTED_REVISION'"
  fi
fi

# ---------------------------------------------------------------- 2. health
STATUS="$(code "$BASE_URL/health")"
if [ "$STATUS" = "200" ] && grep -qE '"status": *"ok"' "$TMP/body"; then
  ok "GET /health -> 200 $(cat "$TMP/body")"
else
  bad "GET /health -> ${STATUS:-<no response>} $(head -c 200 "$TMP/body" 2>/dev/null)"
fi

STATUS="$(code "$BASE_URL/health/deep")"
if [ "$STATUS" = "200" ] && grep -qE '"database": *\{ *"status": *"ok"' "$TMP/body"; then
  ok "GET /health/deep -> 200, database ok"
else
  bad "GET /health/deep -> ${STATUS:-<no response>} $(head -c 300 "$TMP/body" 2>/dev/null)"
fi

# ---------------------------------------------------------------- 3. coupons
STATUS="$(code "$BASE_URL/api/v1/coupons?active_only=true&limit=3")"
TOTAL="$(grep -oE '"total": *[0-9]+' "$TMP/body" 2>/dev/null | head -1 | tr -d ' ' | cut -d: -f2)"
if [ "$STATUS" = "200" ] && [ -n "$TOTAL" ] && [ "$TOTAL" -gt 0 ] 2>/dev/null; then
  ok "GET /api/v1/coupons -> 200, total=$TOTAL"
else
  bad "GET /api/v1/coupons -> ${STATUS:-<no response>} total=${TOTAL:-<none>}"
fi

# ------------------------------------------------- 4. flags that must be off
# Behavioural proof that PERSONALIZATION_ENABLED is off: every personalization
# endpoint answers 404 and nothing is computed or stored.
for path in picked-for-you continue ending-soon-saved; do
  STATUS="$(code "$BASE_URL/api/v1/personalization/$path")"
  if [ "$STATUS" = "404" ]; then
    ok "flag off: GET /api/v1/personalization/$path -> 404"
  else
    bad "flag off: GET /api/v1/personalization/$path -> ${STATUS:-<no response>} (expected 404)"
  fi
done
note "config-level flags (RANKING_VERSION=v1, EXPLORATION_ENABLED=false,"
note "INGESTION_QUARANTINE_MODE=shadow, PERSONALIZATION_* off) are container"
note "settings, not API state — check them in the VM .env, not over HTTP."

# ------------------------------------------- 5. analytics consent (runbook)
# docs/ANALYTICS_VERIFY.md, preflight. An OPTIONS request never reaches a
# handler, so it writes nothing.
STATUS="$(curl -sS -o /dev/null -D "$TMP/preflight" -w '%{http_code}' --max-time 20 \
  -X OPTIONS "$BASE_URL/api/v1/analytics/event" \
  -H "Origin: $EXPECTED_ORIGIN" \
  -H 'Access-Control-Request-Method: POST' \
  -H 'Access-Control-Request-Headers: content-type,x-session-id' 2>/dev/null)"
if [ "$STATUS" = "400" ]; then
  # The API answers 400 for an origin that is not in CORS_ORIGINS, and omits
  # access-control-allow-origin. That is a configuration problem, not a code
  # problem: the browser would refuse the call.
  bad "CORS preflight for '$EXPECTED_ORIGIN' -> 400 (origin not allowed; add it to CORS_ORIGINS in the VM .env)"
elif [ "$STATUS" = "200" ] || [ "$STATUS" = "204" ]; then
  if grep -qi "access-control-allow-origin: *$EXPECTED_ORIGIN" "$TMP/preflight" \
     && grep -qi 'access-control-allow-credentials: true' "$TMP/preflight"; then
    ok "CORS preflight for '$EXPECTED_ORIGIN' -> $STATUS, allow-origin + allow-credentials"
  else
    bad "CORS preflight -> $STATUS but the headers are wrong ($(grep -i '^access-control' "$TMP/preflight" | tr '\n' ' '))"
  fi
else
  bad "CORS preflight for '$EXPECTED_ORIGIN' -> ${STATUS:-<no response>}"
fi

# Consent "declined" only deletes the visitor/session cookies; it stores
# nothing, so it is safe to run here.
STATUS="$(code -X POST "$BASE_URL/api/v1/analytics/consent" \
  -H 'Content-Type: application/json' -d '{"choice":"declined"}')"
if [ "$STATUS" = "204" ]; then
  ok "POST /api/v1/analytics/consent {declined} -> 204 (clears cz_vid/cz_sid, stores nothing)"
else
  bad "POST /api/v1/analytics/consent {declined} -> ${STATUS:-<no response>} (expected 204)"
fi
note "the 'accepted' path in docs/ANALYTICS_VERIFY.md writes one analytics_events"
note "row by design, so this script does not run it. To check it by hand:"
note "  curl -i -X POST $BASE_URL/api/v1/analytics/event -H 'Content-Type: application/json' \\"
note "    -H 'x-session-id: <uuid>' -d '{\"event\":\"page_view\",\"page_type\":\"homepage\"}'"
note "then re-run the counts below and compare."

# ----------------------------------------------- 6. read-only SQL counts
if [ "$SKIP_CONTAINER_CHECKS" = "1" ]; then
  note "SQL counts — skipped (SKIP_CONTAINER_CHECKS=1)"
elif ! command -v $COMPOSE >/dev/null 2>&1; then
  note "SQL counts — skipped (no '$COMPOSE' on PATH)"
else
  COUNTS="$($COMPOSE_CMD exec -T app python - <<'PY' 2>/dev/null
import asyncio
from sqlalchemy import text
from app.core.database import AsyncSessionLocal

async def main():
    async with AsyncSessionLocal() as db:
        for table in ("analytics_sessions", "analytics_events", "coupon_impressions"):
            count = (await db.execute(text(f"select count(*) from {table}"))).scalar_one()
            print(f"{table}={count}")

asyncio.run(main())
PY
)"
  if [ -z "$COUNTS" ]; then
    bad "SQL counts — could not read the counts through the container"
  else
    while IFS= read -r line; do
      [ -z "$line" ] && continue
      ok "count $line"
    done <<< "$COUNTS"
  fi
fi

say ""
say "passed: $PASS   failed: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
