# Launch checklist — Durza

**Written:** 2026-10-22, against `main`:
`durza` = `44aace7` · `durza-web` = `edeffb9` · `durza-ops` = `b0f1be2`.

This is the one page that answers "what is done, what do I check, what has to
happen before Diwali, and what is still on the list". It replaces nothing —
`docs/STATUS.md` and `docs/RUNBOOK.md` are the detailed references, and the
other two repositories have their own copies — but it is the short version,
in the order you will actually need it.

---

## 1. What is done

| Area | Where | State |
|---|---|---|
| Phases 0–4b: foundation, analytics, `/go` attribution, sale-hub backend, ingestion quarantine, ranking trust and exploration | `durza` | merged, deployed |
| Phase 5a–5e: interest store, segments, geo/payment, personalized module, experiments and account settings | `durza`, `durza-web` | merged, deployed |
| Schema reconciliation (`b1c2d3e4f5a6`) — FKs on `coupon_staging`, `now()` defaults, four missing indexes | `durza` | applied in production 2026-10-21 |
| Migration guard: `downgrade`/`stamp` refused in production, `upgrade` needs a per-run flag | `app/core/migration_guard.py` | merged, deployed |
| Deploy pipeline: migrate before start, per-run flag, image prune, change detection against the last **successful** deploy, queued and uncancellable deploys | `.github/workflows/ci.yml` | merged, deployed |
| Sale hubs in the back office: admin views for series, editions and rules, a **Publish** action, and the API resolving an edition's rules to its coupons | `durza#14` → `3cec7ff` | merged, deployed |
| Privacy: `IP_HASH_SALT` (falls back to the secret key), nullable `coupon_verifications.ip_hash` scrubbed after 90 days, signed HttpOnly consent cookie scoped to the parent domain | `durza#15` → `a0052cc`, migration `f7a8b9c0d1e2` | merged, deployed; the migration was applied in production by that deploy |
| The email sender name is "Durza Coupons", with a test | `durza#6` → `5e354e8`, `durza#5` → `b1f6fd6` | merged, deployed |
| The "For you" row is behind `NEXT_PUBLIC_PERSONALIZATION_ENABLED` and the personalization purpose, with a "Why am I seeing this?" control | `durza-web#10` → `617a1db` | merged, deployed |
| The storefront tells the API the analytics choice, so the server can issue the consent cookie | `durza-web#11` → `713be64` | merged, deployed |
| Sale-hub pages show the coupons their rules resolve to | `durza-web#9` → `4f6f480` | merged, deployed |
| Ops: `db backup` (custom-format dump, 14 daily + 8 weekly retention, off-host placeholder) and `db restore-drill` (table and ledger row counts, fails on a mismatch) | `durza-ops` | merged |
| Operator status and runbook for all three repositories | `durza#13`, `durza-web#8`, `durza-ops#3` | merged |

**Every flag is off.** `PERSONALIZATION_ENABLED`, `NEXT_PUBLIC_PERSONALIZATION_ENABLED`,
`NEXT_PUBLIC_PERSONALIZATION_EXPERIMENTS_ENABLED`, `NEXT_PUBLIC_SALE_HUBS_ENABLED`,
`SCHEDULER_ENABLED`, `EXPLORATION_ENABLED` — all at their default (off/false).
The live site shows no personalization surface and no sale-hub page.

**The production database is at `f7a8b9c0d1e2`.** The running code is
`a0052cc` — the last merge that changed the artefact. The merge after it was
documentation only, and the change detection correctly decided not to redeploy.

---

## 2. What you must verify

### 2.1 The schema, in Neon

```sql
SELECT version_num FROM alembic_version;
-- expect: f7a8b9c0d1e2
```

### 2.2 The running service

```bash
curl -s https://api.durza.in/health          # {"status":"ok","environment":"production"}
curl -s -o /dev/null -w '%{http_code}\n' https://durza.in/     # 200
```

### 2.3 The consent flow, in a real browser

This is the one that has no read-only shortcut: the point of the change is
that the server only records an event when it can verify a cookie it signed,
so it has to be checked from a browser that actually made the choice.

**The flow, exactly:**

| Piece | Value |
|---|---|
| The endpoint that issues the cookie | `POST /api/v1/analytics/consent` on the API (`api.durza.in/api/v1`) |
| Request body | `{"choice": "accepted"}` or `{"choice": "declined"}` |
| Response | `204 No Content`; on accept it sets the cookie, on decline it deletes it |
| Cookie name | `cz_consent` (setting `ANALYTICS_CONSENT_COOKIE`, default `cz_consent`) |
| Cookie value | `accepted.<issued-epoch>.<hmac-sha256 hex>` — signed with `SECRET_KEY` |
| Domain | the parent domain of `SITE_URL` — `.durza.in` if `SITE_URL` is a subdomain of it, host-only otherwise. **Check the `Domain` attribute in DevTools; it tells you which it is.** |
| Lifetime | `ANALYTICS_CONSENT_MAX_AGE_DAYS`, default **365** days (`Max-Age=31536000`) |
| Attributes | `HttpOnly`, `SameSite=Lax`, `Path=/`, `Secure` in production |
| Who calls it | `lib/consent.ts` → `setConsent()` in the storefront, on **both** choices, with `credentials: "include"`, fire-and-forget |
| What the server does with it | `POST /analytics/event` and `POST /analytics/impression` store **nothing** unless the cookie verifies. Both still answer **202**. A tampered, expired or missing cookie is "no consent". |
| On decline | the endpoint deletes `cz_consent`, `cz_vid` and `cz_sid` |

**In the browser:**

1. Open `https://durza.in/` in a window with no cookies for it (or clear
   cookies first). The consent banner appears.
2. Click **Accept analytics** (the consent banner's button; the account
   settings page has the same choice under *Consent preferences*).
3. **Network tab → filter `consent`.** You should see
   `POST https://api.durza.in/api/v1/analytics/consent` → `204`, and the
   response headers must contain:
   `Set-Cookie: cz_consent=accepted.…; Max-Age=31536000; Path=/; HttpOnly; SameSite=lax; Secure`
   (`Domain=.durza.in` if the site URL is a subdomain of `durza.in`).
4. **Application tab → Cookies → `https://api.durza.in`.** `cz_consent` is
   there, marked HttpOnly. Type `document.cookie` in the console: `cz_consent`
   is **not** in it — that is the point of HttpOnly.
5. **Produce an event.** Copy a coupon code (the `copy_code` event) and open
   a coupon listing (an impression). In the Network tab: `POST
   .../analytics/event` → `202`, and `POST .../analytics/impression` → `202`.
6. **Check that rows were written** (in Neon):
   ```sql
   SELECT count(*) FROM analytics_events    WHERE created_at > now() - interval '15 minutes';
   SELECT count(*) FROM coupon_impressions  WHERE created_at > now() - interval '15 minutes';
   ```
   Both should be greater than zero.
7. **The negative check.** Clear the `cz_consent` cookie (or use a fresh
   private window and decline), repeat step 5, and re-run the counts. The
   endpoints still answer `202`, and the counts must **not** move. That is
   the whole change: a request without consent is accepted and dropped.

### 2.4 The sale hub, before it goes live

```bash
curl -s https://api.durza.in/api/v1/sales | head -c 400
curl -s https://api.durza.in/api/v1/sales/<series-slug> | head -c 400
curl -s https://api.durza.in/api/v1/sales/<series-slug>/<edition-slug> | head -c 400
```

The last one should list the coupons the rules resolved to.

---

## 3. What must happen before Diwali

In this order. Steps 1–5 are content; step 6 is the only configuration change,
and it is the only one that makes a hub visible.

1. **Publish the guide.** The hub points at a piece of editorial content, and
   that content must be published first. A draft guide means a refused
   publish — by design, not by bug.
2. **Create the series** — `/admin` → *Sale series*. `slug` `diwali-2026`
   (the URL, so pick it once), `title`, a one-line `description`.
3. **Create the edition** — `/admin` → *Sale editions*. The dates, the year
   `2026`, the `source_url` the dates came from, the published guide it
   points at, `lifecycle_status = announced`.
4. **Add the rules** — `/admin` → *Sale coupon rules*. One per store slug
   (`croma`) or category slug. Up to 20 per edition. A rule naming a store
   or category that does not exist is ignored; a `tag` rule resolves to
   nothing, because a coupon has no tag.
5. **Publish** — select the edition → **Publish**. The service refuses an
   edition whose guide is not published or whose confirmed dates have no
   source. On success `publication_status` becomes `published`.
6. **Set `lifecycle_status = live`** on the edition. The lifecycle
   (`announced` → `upcoming` → `live` → `ended` → `archived`) follows the
   dates only when the scheduler is on, and the scheduler is **off** by
   default. A published `announced` edition is still served as the current
   one, so the hub is reachable either way — but set it to `live` on the day.
7. **Turn on the storefront flag** — `NEXT_PUBLIC_SALE_HUBS_ENABLED=true` on
   the web app, then deploy the web app. It is a `NEXT_PUBLIC_` variable, so
   it is baked in at build time: a rebuild, not a restart. While it is off,
   every `/sales` route answers 404.
8. **Check it** — the three `curl`s in 2.4, and open
   `https://durza.in/sales/diwali-2026` in a browser.

The exact fields for every step: `docs/SALE_HUBS.md`.

---

## 4. The deferred backlog

Everything below is recorded, not forgotten.

| # | Item | Where it is recorded |
|---|---|---|
| 1 | The ops skill `event_hub_refresh` (phase 2b) — not built. A hub is created and published by hand, which is enough for a dated launch; the skill is for keeping a long-running hub fresh | `durza-ops/docs/STATUS.md` |
| 2 | The identifier columns that are **not** scrubbed: `analytics_events`, `analytics_sessions`, `coupon_impressions` and `attribution` keep their visitor and session hashes. The click and verification hashes *are* scrubbed | `durza/docs/STATUS.md`; the full inventory is in `durza#15` |
| 3 | `RANKING_VERSION` stays `v1`: `quality_score_v2` is a shadow column with **no writer**, so switching would remove the ranking rather than change it | `docs/RANK_DIFF_v1_v2.md` |
| 4 | The compose switch to `docker-compose.prod.yml` — not started | `docs/COMPOSE_SWITCH_PLAN.md` |
| 5 | Ops scheduling — where ops runs unattended, and on which host its database lives; `OPS_BACKUP_OFFHOST` is still a placeholder | `durza-ops/docs/UNATTENDED_RUNNING.md`, `durza-ops/docs/BACKUP.md` |
| 6 | GSC credentials — blocked on `GSC_CREDENTIALS_FILE` and `GSC_SITE_URL` | all three `docs/STATUS.md` |
| 7 | Real-browser E2E and CLS checks, before any personalization flag is turned on | `durza-web/docs/STATUS.md` |
| 8 | The geo helper (state-level, consent-gated, overridable) and the payment picker (not wired) | `durza-web/docs/STATUS.md` |
| 9 | Phases 6–8 | all three `docs/STATUS.md` |
| 10 | Personalization wiring — the module, the controls and the sync are built and hidden; turning them on is a separate, deliberate decision | all three `docs/STATUS.md` |
| 11 | The ops `content_queue` table lives in `public` while the ORM expects `durza_ops`; the backup covers it explicitly, the schema should be reconciled | `durza-ops/docs/STATUS.md` |

---

## 5. Two settings that break the site quietly if they are wrong

### `CORS_ORIGINS` — both origins, always

The API answers browser requests only for the origins it lists
(`app/main.py`: `allow_origins=settings.cors_origin_list`, and
`allow_credentials=True`). The storefront is served from **two** origins, and
a request from an origin that is not on the list is blocked in the browser
before the API ever sees it.

```
CORS_ORIGINS=https://durza.in,https://www.durza.in
```

Both, comma-separated, no trailing slash, no spaces. Leaving out
`https://www.durza.in` means the consent call fails from the www host: no
`cz_consent` cookie is issued, no analytics are recorded, and the only
symptom is a CORS error in the browser console. Leaving out
`https://durza.in` does the same for the apex host.

This matters more than a usual CORS setting because `allow_credentials=True`:
the consent cookie is a credential, and a wildcard origin cannot be used with
credentials. The exact origin has to be listed.

### Rotating `SECRET_KEY` invalidates four things at once

`SECRET_KEY` signs the login tokens (`app/core/security.py:31`), the consent
cookie (`app/core/consent.py`, used at `app/routers/api/v1/analytics.py:229`
and `:256`), the admin session (`app/admin/auth.py:37`) and — when
`IP_HASH_SALT` is not set — the IP hashes (`app/core/request_meta.py:29`).

| What breaks | Who notices | How soon |
|---|---|---|
| Every signed-in user is signed out; their access token no longer verifies | everyone | immediately |
| Every consent cookie stops verifying, so analytics stop being recorded until the browser asks for a new one | the analytics counters | quietly, over the next visits |
| The admin session is invalidated | the operator | on the next admin page |
| With no `IP_HASH_SALT`, every IP hash changes and the rate limiters start from zero | the abuse protection | quietly |

Set **`IP_HASH_SALT` to its own value before you ever rotate `SECRET_KEY`** —
that keeps the hashes out of it. And expect a signed-out wave plus an
analytics dip when you rotate; the storefront re-issues the consent cookie on
load, so the analytics recover on their own, the sign-ins do not.

## 6. If something has to be undone

- **A bad deploy:** `docs/ROLLBACK.md` and `docs/POST_DEPLOY.md`. A rollback
  needs a restore point; take one first.
- **A flag:** every flag is a setting. Turning one off is a rebuild (web) or
  a restart (backend) — no code change, no migration.
- **The privacy migration:** `f7a8b9c0d1e2` is additive and reversible in the
  sense that its downgrade restores `NOT NULL` — but only by writing
  `'scrubbed'` into the rows the scrub has already nulled. The original
  hashes are gone either way, and that is the point of the scrub.
