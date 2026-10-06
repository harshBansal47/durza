# Savevero — Phases 0–5 (backend)

Bootstrap (Phase 0), core infra (Phase 1), auth + RBAC (Phase 2), the core
domain API (Phase 3), the admin back office (Phase 4), and now the public-API
foundation for the frontend (Phase 5): commission disclosure, community
verification, and a redirect endpoint that finally hides `destination_url`
from anonymous requests. The actual public website lives in the sibling
`couponza-web` project (Next.js) and calls this API — nothing here renders
HTML for visitors.

## Prerequisites

- Docker and Docker Compose installed locally.

## First run

```bash
cp .env.example .env
docker compose up --build
```

Then open:

- http://localhost:8000/health — should return `{"status": "ok", "environment": "development"}`
- http://localhost:8000/docs — interactive API docs (empty for now, but confirms FastAPI is serving)

## Running migrations

The committed migrations (`alembic/versions/71d3354b511b_initial_schema.py`,
then `1f2b1639c23d_sources_ingestion_runs_coupon_status.py`) have both been
verified to apply cleanly. Just apply them:

```bash
docker compose exec app alembic upgrade head
```

If you add or change a model afterward, generate a new migration the normal way:

```bash
docker compose exec app alembic revision --autogenerate -m "describe the change"
docker compose exec app alembic upgrade head
```

## Signing in to the back office

Open http://localhost:8000/admin — sign in with the admin account created by
`seed_admin.py` below. Editors can sign in too, with reduced permissions (see
the table below). Regular `user`-role accounts cannot sign in at all.

## Creating the first admin user

There is no public "become an admin" endpoint on purpose — seed it directly:

```bash
docker compose exec app python scripts/seed_admin.py --email admin@example.com --password ChangeMe123!
```

## Trying the auth flow

```bash
# Register (always creates a plain "user")
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "alice@example.com", "password": "supersecret123"}'

# Log in (OAuth2 password flow — form-encoded, "username" is the email)
curl -X POST http://localhost:8000/api/v1/auth/login \
  -d "username=alice@example.com&password=supersecret123"

# Call a protected route
curl http://localhost:8000/api/v1/auth/me -H "Authorization: Bearer <access_token>"

# Refresh
curl -X POST http://localhost:8000/api/v1/auth/refresh \
  -H "Content-Type: application/json" \
  -d '{"refresh_token": "<refresh_token>"}'

# Admin-only demo route (403 for a regular user, 200 for an admin)
curl http://localhost:8000/api/v1/auth/admin-ping -H "Authorization: Bearer <access_token>"
```

## Running tests

```bash
docker compose exec app pytest
```

## Phase 5: trust, verification, and the public-safe API

Three additions, driven directly by the product-strategy discussion (Honey's
2026 affiliate-commission scandal, and AI agents increasingly doing the
coupon-hunting themselves):

- **Commission disclosure.** `Store.commission_disclosure` is a free-text field
  admins/editors set per store (e.g. "We earn ~4% from Amazon on this link").
  `StoreRead` exposes it publicly. Null falls back to a generic disclosure in
  the frontend — there's no store where a commission is silently undisclosed.
- **Community verification.** `POST /coupons/{id}/verify` with `{"worked": bool}`
  records an anonymous report, rate-limited to one per coupon per IP per 24h
  (enforced via a salted IP hash in the new `coupon_verifications` table — the
  raw IP is never stored). `Coupon.success_count` / `fail_count` /
  `last_verified_at` are denormalized onto the coupon for fast reads; the
  `success_rate` field is computed, not stored.
- **The public-safe view.** Every public `GET` on `/coupons` now returns
  `CouponPublicRead`, which has no `destination_url` field at all. The only way
  to actually reach a store is `GET /coupons/{id}/go`, which logs a click and
  302-redirects. Staff-only create/update responses (`CouponRead`) still
  include `destination_url`, since whoever just typed it in needs to see it.

## Security hardening (added after an honest audit)

Four gaps that "the backend is done" glossed over, now fixed:

- **CORS.** Without this, a browser silently blocks every client-side fetch
  the Next.js frontend makes to this API — the community verification widget
  specifically, since that's the one thing that runs client-side rather than
  server-side. Configure allowed origins via `CORS_ORIGINS` (comma-separated)
  in `.env`; defaults to `http://localhost:3000` for local dev.
- **Rate limiting on auth.** `/auth/register` (5/min), `/auth/login` (10/min),
  and `/auth/refresh` (20/min) are now throttled per IP via slowapi, on top of
  the existing per-coupon 24h cooldown on `/coupons/{id}/verify`. Without
  this, login had zero brute-force protection. In-memory by default — fine
  for one instance; point `Limiter` at Redis (`app/core/limiter.py`) if you
  run more than one API process.
- **Security headers.** `X-Content-Type-Options`, `X-Frame-Options`,
  `Referrer-Policy` on every response; `Strict-Transport-Security` added
  automatically once `ENVIRONMENT=production`.
- **Non-root Docker user.** The container previously ran as root. It now
  drops to a `appuser` (UID 1000, matching the typical first non-root Linux
  user, so bind-mounted volumes in local dev don't hit permission errors).

**Still worth knowing, not yet built:** no email verification on
registration (anyone can register with any email address, confirmed or not),
no account lockout after repeated failed logins beyond the per-IP rate limit,
no dependency vulnerability scanning wired into CI (no CI exists yet at all),
and the non-root Docker change is untested against a real `docker compose up`
in this environment — Docker itself isn't available here, so it's reviewed
carefully but not run.

## Priority #3, completed: the MCP server

An MCP server (`mcp_server/`) exposes the same verified data as the JSON API,
but as tools any MCP-compatible agent (Claude, or others) can call directly —
rather than needing to scrape rendered HTML the way a generic web agent
would. It's a thin layer: `mcp_server/client.py` talks to this API over plain
HTTP, the same way `couponza-web` does; no database access, no duplicated
business logic.

**Tools exposed:** `search_coupons`, `get_coupon`, `get_store`,
`list_categories`, `report_coupon_result` (yes — an agent that actually tries
a code can report back whether it worked, feeding the same community
verification signal a human clicking "Worked" / "Didn't work" would).

**Run it:**

```bash
pip install -e ".[mcp]"
export SAVEVERO_API_URL=http://localhost:8000/api/v1
python -m mcp_server                    # stdio transport (for a local client)
MCP_TRANSPORT=streamable-http python -m mcp_server   # for a remote deployment
```

**Connect it to Claude Desktop** by adding to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "savevero": {
      "command": "python",
      "args": ["-m", "mcp_server"],
      "env": { "SAVEVERO_API_URL": "http://localhost:8000/api/v1" }
    }
  }
}
```

**Deliberate design choice:** the server's `instructions` field explicitly
tells the agent to weigh `success_rate` and `last_verified_at`, and to
surface `commission_disclosure` whenever it recommends a coupon — the same
trust commitments made to a human visitor apply to whatever an agent tells
its user, not just to the rendered page.

## Frontend

The public website is a separate project: `../couponza-web` (Next.js). It
calls this API's public endpoints directly and never touches the database.
See its own README for setup.

## What's here

| Path | Purpose |
|---|---|
| `app/main.py` | FastAPI app instance, mounts the auth router, `/health` endpoint |
| `app/core/config.py` | Typed settings loaded from `.env` |
| `app/core/database.py` | Async SQLAlchemy engine/session + declarative `Base` |
| `app/core/security.py` | Password hashing (bcrypt) and JWT issue/verify |
| `app/core/deps.py` | `get_current_user` and the `require_role(...)` dependency factory |
| `app/models/base.py` | `TimestampMixin` and `UUIDPKMixin` for every model |
| `app/core/pagination.py` | `Paginated[T]` envelope + `paginate()` helper |
| `app/core/slugs.py` | Unique slug generation shared by all sluggable models |
| `app/models/` | `user`, `category`, `store`, `coupon`, `ad`, `page` |
| `app/schemas/` | Pydantic Create/Update/Read schemas per entity |
| `app/services/` | Business logic per entity, kept out of the routers |
| `app/routers/api/v1/` | `auth`, `categories`, `stores`, `coupons`, `ads`, `pages` |
| `app/admin/auth.py` | Login/session handling + admin-vs-editor authorization rules |
| `app/admin/views.py` | One `ModelView` per entity — the actual admin forms/lists |
| `app/admin/setup.py` | Mounts everything at `/admin` (called from `app/main.py`) |
| `app/models/coupon_verification.py` | One row per community "worked"/"didn't work" report |
| `app/core/request_meta.py` | Client-IP extraction + salted hashing for rate limiting |
| `app/core/limiter.py` | Shared slowapi rate limiter |
| `app/core/security_headers.py` | Adds security headers to every response |
| `mcp_server/client.py` | Thin HTTP client over the public API, reused by both the server and its tests |
| `mcp_server/server.py` | The 5 MCP tools agents can call |
| `scripts/seed_admin.py` | Creates the first admin user (no public signup path for that role) |
| `alembic/` | Async-aware migration environment |
| `tests/` | 80 tests: health, auth/RBAC, CRUD for every entity, the full admin panel, public-safe/verification/redirect behavior, CORS/rate-limiting/security-headers, and the MCP server (SQLite in-memory) |

## What the admin can do (Phase 4)

Mounted at `/admin` via [SQLAdmin](https://aminalaee.dev/sqladmin/), using the
**same** `User` table and password hashes as the JSON API — one set of
credentials for both.

| | Admin | Editor | Plain user |
|---|---|---|---|
| Sign in to `/admin` at all | ✅ | ✅ | ❌ |
| Create/edit Categories, Stores, Coupons, Ads, Pages | ✅ | ✅ | — |
| Delete Categories, Stores, Coupons, Ads, Pages | ✅ | ❌ | — |
| See/manage the Users list | ✅ | ❌ (hidden from the menu) | — |

**Behaviours worth knowing**
- **Form saves reuse the real logic**, not a copy of it: `on_model_change`
  calls the same `generate_unique_slug()` the API uses, so a category
  created in the admin and one created via `POST /api/v1/categories` get
  slugs the same way. Creating a coupon in the admin also stamps
  `created_by` with whoever is logged in, same as the API.
- **Naive datetimes are rejected**, not silently accepted. The admin's date
  picker submits a plain `2030-01-01 12:00:00` with no timezone; Postgres
  (via asyncpg) refuses to store that in a `timestamptz` column, so both the
  admin (`ensure_utc`) and the API (`AwareDatetime` schemas) normalize or
  reject naive input consistently instead of one path silently defaulting to
  UTC and the other erroring.
- **A signed-in admin can't lock themselves out.** Demoting, deactivating, or
  deleting the account you're currently using is blocked with a clear error.
- **Session cookies are signed with `SECRET_KEY`.** `Settings` refuses to
  boot with `ENVIRONMENT=production` while `SECRET_KEY` is still the
  placeholder value from `.env.example` — set a real one before deploying.
- **Passwords never round-trip through the UI.** The password field only
  writes a new bcrypt hash; leaving it blank while editing a user keeps
  their existing password, and the hash itself is excluded from the
  details view, the list view, and CSV export.

## API overview (Phase 3)

Everything is browsable and testable at http://localhost:8000/docs.

| Resource | Endpoints | Notes |
|---|---|---|
| Categories | `GET/POST /categories`, `GET/PATCH/DELETE /categories/{id}`, `GET /categories/by-slug/{slug}` | Self-referencing `parent_id` gives subcategories; filter with `?parent_id=` |
| Stores | same shape under `/stores` | `?search=` filters by name |
| Coupons | same shape under `/coupons` | Filters: `store_id`, `category_id`, `search`, `active_only` (default true) |
| Ads | `/ads` (no by-slug) | Filters: `position`, `active_only` |
| Pages | `/pages` | `?published_only=` (default true) |

**Access policy:** reads are public; create/update need `admin` or `editor`;
delete needs `admin`. Every list endpoint returns
`{"items": [...], "total": N, "skip": 0, "limit": 20}` and accepts `skip`/`limit`
(limit max 100).

**Behaviours worth knowing**
- Slugs are generated from the title/name and made unique automatically
  (`fashion`, `fashion-2`, ...). Renaming an item regenerates its slug.
- Creating a coupon with an unknown `store_id`/`category_id` returns a clean
  `404` rather than a database error.
- `Coupon.destination_url` is currently returned by the API (admins need it).
  Phase 5 adds a public-safe view plus the `/go/{id}` click-through redirect so
  visitors never see the raw affiliate link.

## Design notes worth knowing

- **Why no public admin signup?** `/register` always creates a `user` role.
  Promoting someone to admin/editor is a deliberate, out-of-band action via
  the seed script (or, later, an existing admin using the admin panel) —
  never something a request body can grant itself.
- **Access vs refresh tokens** carry a `"type"` claim and each endpoint checks
  it, so a leaked access token can't be replayed against `/refresh`.
- **Tests use SQLite in-memory** (with foreign keys switched on to mimic Postgres), not Postgres, via a `get_db` dependency
  override in `tests/conftest.py` — fast, no Docker needed to run `pytest`
  locally. The `Uuid`/`Enum` column types were chosen specifically because
  they're dialect-agnostic (work on both Postgres and SQLite).

## Coupon data + verification engine (`app/ingestion/`)

The machinery to *continuously acquire, validate, expire, and refresh* offers:

```
Source → adapter (fetch RawOffers) → normalize → validate → dedupe →
(optional) HTTP probe of destination_url → upsert Coupon (status=active/failed/expired)
→ IngestionRun audit row → public API + MCP (only active coupons are served)
```

- **`Source`** (new model): a place offers come from. `kind` picks the adapter —
  `csv` (a CSV export at `config.path`) or `static` (offers embedded in config,
  for tests/seed data). `probe_destinations=true` turns on live URL checks.
- **`IngestionRun`** (new model): one row per pipeline execution, with
  fetched/created/updated/skipped/failed counts and any error — viewable in
  `/admin` under "Ingestion Runs".
- **`Coupon.status`** (`active`/`failed`/`expired`) is new; `is_active` is kept
  in sync so all existing public filters behave unchanged. Coupons also gain
  `source_id`, `external_id`, `failure_reason`, `last_checked_at`, `content_hash`.
- **Dedupe identity** is sha256 of `(store_slug, code-or-title)` — a changed
  discount *updates* the existing coupon instead of creating a duplicate.
  `(source_id, external_id)` matches first when the source provides a stable id.
- **Validation** rejects bad URLs, negative discounts, out-of-range percentages,
  over-long codes. Offers with past `expires_at` are upserted as `expired`.
- **Probe** (`app/ingestion/probe.py`) does an httpx GET with redirects and a
  10s timeout; failures mark the coupon `failed` with the reason.

Run it with:

```bash
docker compose exec app python scripts/run_ingestion.py --source-slug my-feed
docker compose exec app python scripts/refresh_coupons.py          # expire + re-ingest all
docker compose exec app python scripts/refresh_coupons.py --skip-sources  # expire only
```

`refresh_coupons.py` is designed for cron (`0 */6 * * * ...`). Adding a new
real source = one `Source` row + (if it isn't CSV) one new adapter in
`app/ingestion/sources/` + one line in `app/ingestion/registry.py`.

## Price/Deal intelligence layer (`app/services/price_service.py`)

Phase 1 of the shift from coupon directory to commerce-intelligence platform:

- **`Product`** (new model, FK to `Store` + `Category`) — a merchant's sellable
  unit. Carries the denormalized stats: `current_price`, `list_price`,
  `in_stock`, `last_captured_at`, `lowest_price_7d/30d/90d`, and
  `last_price_drop_at/pct`.
- **`PricePoint`** (new model) — one observed price at a moment: `price`,
  `original_price`, `shipping`, `in_stock`, optional `coupon_id`, `captured_at`.
  This is the `price_history[]`; the product's rolling lows are recomputed from
  it on every insert.
- **Effective price** — `GET /api/v1/products/{id}` returns `effective_price`
  computed from the latest point's linked coupon (percentage/fixed discounts
  applied, shipping added, floored at 0). This is the "\u20b9Coupon: \u20b91,000 OFF → Effective: \u20b97,499"
  number the frontend can show without duplicating the math.
- **Price-drop detection** — when a new point comes in below the previous
  price, `last_price_drop_pct` + `last_price_drop_at` are stored (used later
  for alerts / deal-quality).
- Public read endpoints: `GET /products`, `/products/{id}`, `/products/by-slug/{slug}`,
  `/products/{id}/price-history`. Writing points requires admin/editor.

Frontend pages/charts for products are the next step (no product UI exists yet).

## User retention + distribution (Phase 1)

The retention loop: `User ├── saved stores / saved coupons / tracked products
(target price) / notification preferences` → `AlertEvent` history → channels
(email / telegram / push).

- **Endpoints** (all under `/api/v1/me`, bearer token required):
  - `GET|POST|DELETE /me/saved-stores[/{id}]`, same for `/me/saved-coupons`
  - `GET|POST /me/tracked-products`, `PATCH|DELETE /me/tracked-products/{id}`
  - `GET|PATCH /me/notification-preferences` (email on/off, telegram chat id, push subscription)
  - `GET /me/alerts` — what was sent and when
- **Alert engine** (`app/services/alert_service.py`, run via
  `scripts/run_alerts.py` on cron): per tracked product, on new observation:
  `target_met` (effective price incl. coupon ≤ target) beats `price_drop`
  beats `coupon_appeared`. One alert per (tracked product, price point) —
  `TrackAlertState` prevents re-alerting about the same data.
- **Channels** (`app/services/notifications.py`): Telegram is real when
  `TELEGRAM_BOT_TOKEN` + chat id are set; email logs/queues until an SMTP
  client is wired; browser push queues until the extension + VAPID land.
  WhatsApp deliberately not built.
- Idempotent by design: duplicate saves/tracking upsert instead of erroring.

Not built yet (by design, in order): frontend account UI using these endpoints,
web-push delivery, the browser extension itself.

## Next step

- Frontend product page: price-history chart, effective-price block, "90-day low" badge.
- A price-source adapter feeding `record_price` automatically (same `Source`/feed
  machinery as ingestion) — this is also what starts real alert traffic.

## Click tracking and affiliate links

Every outbound click goes through a redirect, so each one is recorded and can be
tied to an affiliate sale later:

- `GET /api/v1/coupons/{id}/go?src=<tag>` and `GET /api/v1/products/{id}/go?src=<tag>`
  record a `ClickEvent`, build the outbound URL for the store's network, and 302.
  `src` is a short tag for the page that sent the click (`coupon-page`, `email`, ...);
  anything that is not a short lowercase tag is stored as `unknown`.
- Each click gets a unique `clickref`, appended to the outbound link
  (`clickref` for Awin, `subid` for Cuelinks/Admitad, `subId1` for Impact). The network
  returns it on its transaction report, which is how revenue is matched to a coupon.
- Per-store settings (admin, never in public JSON): `affiliate_network`, optional
  `link_template` (placeholders `{url}`, `{raw_url}`, `{clickref}`), `cookie_days`.
  See `app/services/affiliate.py`.
- `product.url` is no longer returned by the public API (`has_url` says whether a
  buy link exists); the only way to reach the store is `/products/{id}/go`.
- Privacy: only a salted one-way hash of the IP is stored, and the `scrub_clicks` job
  nulls it after `CLICK_RETENTION_DAYS` (default 90). The click row stays so revenue
  attribution keeps working.
- Rapid repeats from the same visitor within 10 seconds are one click and reuse the
  same reference. Bots are flagged on the row (`is_bot`), not dropped.

## Sponsored placements

Paid banners (`/api/v1/ads`) are built so a shopper can always tell they are paid and
an advertiser can never be served outside what they booked:

- **Always labelled.** `label` is one of `Sponsored` (default), `Partner` or `Ad`;
  blank or free-text labels are rejected, so the disclosure cannot be edited away.
- **Scheduled.** An ad is listed and redirectable only while `is_active` is true and
  now is inside `starts_at`..`ends_at`. `ends_at` must be after `starts_at`.
- **No bypass.** The public JSON has no `target_url`. The advertiser is reached only via
  `GET /api/v1/ads/{id}/go?src=<tag>`, which records a `ClickEvent` (with `ad_id`),
  adds `utm_source=savevero&utm_medium=sponsored&utm_campaign=<ad id>` (the
  advertiser's own UTM values are kept) and 302s. Ads whose stored URL is not http(s)
  never redirect.
- **URLs are validated** on write: `image_url` and `target_url` must be http(s).
- Ad clicks reuse the click pipeline: 10-second dedupe, bot flag, 90-day hash scrub.
  They carry no `store_id` and earn no affiliate commission, so **exclude rows with
  `ad_id` set from per-store earnings reports.**
- Impressions are not counted yet (that needs batching rather than a write per page view).

## Discovery orderings

`GET /api/v1/coupons?sort=<name>` lets the storefront build discovery sections without
inventing signals. Every ordering is backed by data we hold; the frontend only presents it.

| `sort` | Meaning | Excluded |
|---|---|---|
| `newest` (default) | most recently added | |
| `recommended` | the ranking engine's `quality_score`, best first (the score itself is never exposed) | |
| `ending_soon` | soonest real expiry first | no expiry, already expired |
| `recently_verified` | most recently confirmed by a shopper | never verified |
| `trending` | most non-bot outbound clicks in the last 7 days | coupons nobody clicked this week (the list can be empty) |
| `biggest_discount` | highest percentage off | fixed-amount and code-less deals (not comparable without the basket) |
| `most_used` | most all-time clicks (not "trending") | |
| `top_rated` | highest success rate | fewer than 5 reports |

`expires_within_days=N` keeps only coupons that really expire within N days, and
`verified_within_hours=N` only those confirmed within the last N hours. The response `total`
is the exact count for the filter, which is what a "12 verified today" label should use. With
`active_only=true` (default) a coupon is hidden the instant `expires_at` passes, without
waiting for the lifecycle job.

## Shopper events

`POST /api/v1/analytics/event` records interactions that are not impressions (impressions use
`POST /analytics/impression`). The storefront sends a flat JSON object:

```json
{"event": "copy_code", "coupon_id": "<uuid>", "ts": "2026-10-06T10:00:00Z"}
```

- `event` must be one of: `page_view`, `search`, `coupon_click`, `coupon_reveal`, `copy_code`,
  `save_coupon`, `unsave_coupon`, `follow_store`, `unfollow_store`, `merchant_click`,
  `filter_change`, `verify_vote`. Anything else is a 422.
- `coupon_id` / `merchant_id` are stored without foreign keys, and a malformed id only drops that
  field, so one bad value never loses the event.
- Extra keys become `props`: at most 10 short scalar values (strings cut at 200 characters).
  Nested objects and oddly named keys are discarded.
- Visitor comes from the `cz_vid` cookie and session from the `x-session-id` header, as for
  impressions. Rate limit: 120/minute per client.
- For impressions the storefront must send a `page_type` from the `ImpressionType` list and a
  1-based `position`; anything else is rejected with a 422.

