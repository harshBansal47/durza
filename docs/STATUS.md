# Status — Durza backend

**Repository:** `harshBansal47/durza` (`couponza/`)
**Branch state at the time of writing:** `main` = `a0052cc94c69e2cf72f63b8efa5f4e8bed95fb0e`
**Alembic head:** `f7a8b9c0d1e2` — "make coupon verifications' ip hash nullable",
directly on `b1c2d3e4f5a6` ("reconcile schema drift after phase 5"), which is
directly on `5ee40ae51cc1`. One head, no branches. `f7a8b9c0d1e2` was applied
in production on 2026-10-21, by the deploy of `a0052cc`.

## What is merged

| Area | Where | State |
|---|---|---|
| Foundation, analytics, /go attribution, sale hubs, ingestion quarantine, ranking trust and exploration | Phases 0–4b | merged, deployed |
| Phase 5 backend stack (5a interests, 5b segments, 5c geo/payment) | one merge commit `dddded8` | merged, deployed |
| Schema reconciliation (FKs on `coupon_staging`, `now()` defaults, four missing indexes) | `b1c2d3e4f5a6` | applied in production on 2026-10-21 |
| Migration guard (`downgrade`/`stamp` refused in production, `upgrade` needs a per-run flag) | `app/core/migration_guard.py` | merged, deployed |
| Deploy step: migrate before starting the container, per-run flag, image prune, change detection, deploy concurrency | `.github/workflows/ci.yml` | merged, deployed |
| Sale hubs in the back office: admin views for the series, the editions and the coupon rules, a Publish action on an edition, and the API resolving an edition's rules to the coupons it shows | `durza#14` -> `3cec7f` | merged, deployed |
| Privacy: `IP_HASH_SALT` (falling back to the secret key), a nullable `coupon_verifications.ip_hash` scrubbed after `VERIFICATION_RETENTION_DAYS` (90), and a signed HttpOnly consent cookie scoped to the parent domain — without it `/analytics/event` and `/analytics/impression` store nothing and still answer 202 | `durza#15` -> `a0052cc`, migration `f7a8b9c0d1e2` | merged, deployed |
| The email sender name, "Durza Coupons", and its test | `durza#6` -> `5e354e8`, `durza#5` -> `b1f6fd6` | merged, deployed |

## What is scaffolding, with the flags off

Every flag below is **off**. The personalization API answers 404 while
`PERSONALIZATION_ENABLED` is false, and the web hides every personalization
control behind the matching `NEXT_PUBLIC_PERSONALIZATION_ENABLED`.

| Feature | Flag | Default | Where it stops today |
|---|---|---|---|
| Interest store and sync | `PERSONALIZATION_ENABLED` | `false` | `/personalization/*` → 404 |
| Purpose gate | `PERSONALIZATION_PURPOSE_REQUIRED` | `true` | consent is required even if the flag is turned on |
| Segment computation | `PERSONALIZATION_SEGMENT_VERSION` | `1` | no segment is served |
| Signal decay | `PERSONALIZATION_HALF_LIFE_DAYS` | `14` | — |
| Interest TTL | `PERSONALIZATION_TTL_DAYS` | `90` | — |
| Allowlist | `PERSONALIZATION_ALLOWED_CATEGORIES` | `""` (empty) | even switched on, no category passes |
| Hard blocklist | `PERSONALIZATION_BLOCKED_CATEGORIES` | `health,medical,loans,debt,insurance,adult,gambling,alcohol,weapons` | always enforced |
| Signal cap | `PERSONALIZATION_MAX_SIGNALS` | `50` | — |
| Ranking version | `RANKING_VERSION` | `v1` | ranking is unchanged from the pre-4a behaviour |
| Exploration slots | `EXPLORATION_ENABLED` | `false` | no exploration traffic |
| Quarantine mode | `INGESTION_QUARANTINE_MODE` | `shadow` | the detector logs, it never blocks ingestion |
| Scheduler | `SCHEDULER_ENABLED` | `false` | no scheduled job runs |
| Click-hash scrub | `CLICK_RETENTION_DAYS` / `CLICK_SCRUB_INTERVAL_HOURS` | `90` / `24` | only runs with the scheduler on |

Still scaffolding: the `/personalization/*` endpoints, the geo helper (reads a
CDN header, not wired), the payment picker (local only, not wired), the
sale-hub seeds (no regional calendar rows yet), and the content-queue table
that the ops migration created outside the ops schema.

A hub itself is no longer scaffolding in the sense of "you cannot make one":
the back office creates a series, an edition and its rules, and the API
returns the coupons the rules resolve to. What is still missing is the ops
skill `event_hub_refresh` (phase 2b) and a designed hub page — see
`docs/SALE_HUBS.md`.

## Open TODOs (deferred)

1. **The identifier columns that are not scrubbed** — `analytics_events`,
   `analytics_sessions`, `coupon_impressions` and `attribution` still keep
   their visitor and session hashes. The click hashes and the verification
   hashes are scrubbed; these are not. The inventory is in `durza#15`.
3. **Compose switch** — move the deploy host from `docker-compose.yml` to
   `docker-compose.prod.yml` (`docs/COMPOSE_SWITCH_PLAN.md`).
4. **Ops scheduling** — decide where durza-ops runs unattended, and on which
   host its database lives (`durza-ops/docs/UNATTENDED_RUNNING.md`).
5. **Real-browser E2E and CLS checks** — before any personalization flag is
   turned on.
6. **GSC credentials** — still blocked on `GSC_CREDENTIALS_FILE` and
   `GSC_SITE_URL`.
7. **Phases 6–8** — not started.
8. **Personalization wiring** — the module, the controls and the sync are
   built and hidden; turning them on is a separate, deliberate decision.
