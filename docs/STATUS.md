# Status — Durza backend

**Repository:** `harshBansal47/durza` (`couponza/`)
**Branch state at the time of writing:** `main` = `9c17e65591249a9cf72475d3701037f671a535f6`
**Alembic head:** `b1c2d3e4f5a6` — "reconcile schema drift after phase 5", directly on
`5ee40ae51cc1`. One head, no branches. (PR #15 adds `f7a8b9c0d1e2` on top of it —
nullable `coupon_verifications.ip_hash` — and is not merged yet.)

## What is merged

| Area | Where | State |
|---|---|---|
| Foundation, analytics, /go attribution, sale hubs, ingestion quarantine, ranking trust and exploration | Phases 0–4b | merged, deployed |
| Phase 5 backend stack (5a interests, 5b segments, 5c geo/payment) | one merge commit `dddded8` | merged, deployed |
| Schema reconciliation (FKs on `coupon_staging`, `now()` defaults, four missing indexes) | `b1c2d3e4f5a6` | applied in production on 2026-10-21 |
| Migration guard (`downgrade`/`stamp` refused in production, `upgrade` needs a per-run flag) | `app/core/migration_guard.py` | merged, deployed |
| Deploy step: migrate before starting the container, per-run flag, image prune, change detection, deploy concurrency | `.github/workflows/ci.yml` | merged, deployed |

## Open PRs (draft, not merged)

| PR | What it does | State |
|---|---|---|
| `durza#14` | The back office can create and publish a sale hub: admin views for the series, the editions and the coupon rules, a Publish action on an edition, and the API resolving an edition's rules to the coupons it shows. Runbook: `docs/SALE_HUBS.md`. | CI green, not merged |
| `durza#15` | The privacy follow-up: `IP_HASH_SALT` (falling back to the secret key), a nullable `coupon_verifications.ip_hash` scrubbed after `VERIFICATION_RETENTION_DAYS` (90), and a signed HttpOnly consent cookie scoped to the parent domain — without it `/analytics/event` and `/analytics/impression` store nothing and still answer 202. Migration `f7a8b9c0d1e2`. | CI green, not merged |
| `durza#6` | The email sender name, "Durza Coupons" — rebased onto the current `main`. | CI green, not merged |
| `durza#5` | The test for the sender name, stacked on `durza#6` — rebased onto the current `main`. | CI green, not merged |

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
PR #14 adds the back office that creates a series, an edition and its rules,
and the API that returns the coupons the rules resolve to. What is still
missing is the ops skill `event_hub_refresh` (phase 2b) and a designed hub
page — see `docs/SALE_HUBS.md`.

## Open TODOs (deferred)

1. **The privacy follow-up** — `CouponVerification.ip_hash` nullable plus a
   scrub that nulls it after `verification_retention_days` (default 90,
   configurable); an `ip_hash_salt` setting with a fallback to `secret_key`; a
   server-side consent requirement for `POST /api/v1/analytics/event` and
   `/impression` — store nothing without a consent-derived cookie, and still
   answer 202; and the inventory of every identifier column.
   **In flight as PR #15, not merged.** It lands with the identifier
   inventory in the PR description, not here.
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
