# Sale hubs — creating and publishing one

A sale hub is a shelf of deals for a moment: "Diwali 2026". It is a
**series** (the brand or event), one or more **editions** (a dated run of it),
and the **rules** that decide which coupons the edition shows.

Everything is done in the back office at `/admin`. The storefront only shows a
hub that is published, and only while the flag is on.

## Before you start

- The flag is **`NEXT_PUBLIC_SALE_HUBS_ENABLED`** on the web app. It is **off**
  by default, and while it is off every `/sales…` route answers 404. Turning it
  on is a web deploy, not a content change.
- The content a hub points at must be **published** in the CMS first. The
  publish action refuses an edition whose linked guide is not published — that
  is deliberate, not a bug: a hub must not point at a draft.

## 1. Create the series

`/admin` → **Sale series** → *Create*.

| Field | Value for Diwali 2026 |
|---|---|
| `slug` | `diwali-2026` (lowercase, digits and dashes, max 40 characters) |
| `title` | `Diwali 2026` |
| `description` | one line for the hub page |
| `publication_status` | `draft` until the edition is ready |

The slug is the URL: `durza.in/sales/diwali-2026`. It is part of the
contract, so pick it once.

## 2. Create the edition

**Sale editions** → *Create*.

| Field | Value |
|---|---|
| `series` | the series from step 1 |
| `slug` | `diwali-2026` (max 30 characters) |
| `year` | `2026` |
| `start_at` / `end_at` | when the sale runs |
| `dates_confirmed` | tick it only when the dates are known |
| `source_url` | required when `dates_confirmed` is ticked — where the dates come from |
| `editorial_content_id` | the published guide (or comparison) for this sale |
| `lifecycle_status` | `announced` until the dates arrive |
| `publication_status` | `draft` |

## 3. Add the rules — what the hub shows

**Sale coupon rules** → *Create*, one per rule:

| `rule_type` | `rule_value` | Meaning |
|---|---|---|
| `store` | the store's slug, e.g. `croma` | the coupons of that store |
| `category` | the category's slug | the coupons in that category |
| `tag` | a tag | **not resolved yet** — there is no tag on a coupon, so a tag rule shows nothing |

A rule that names a store or category that does not exist is ignored rather
than guessed at, so check the hub page shows the coupons you expect.

Up to 20 rules per edition. The hub shows the active coupons those rules
resolve to, soonest expiry first.

## 4. Publish

**Sale editions** → select the edition → **Publish**.

The service is the judge. It refuses when:

- the linked editorial content is not published;
- `dates_confirmed` is ticked but there is no `source_url`.

On success the edition's `publication_status` becomes `published`. The
series must be published too (`publication_status = published` on the series)
before the hub is reachable at all.

## 5. The lifecycle

`announced` → `upcoming` → `live` → `ended` → `archived`, driven by
`start_at`/`end_at` once the dates are confirmed. The transition job only
runs when the scheduler is on (`SCHEDULER_ENABLED`, off by default), so for a
hub that has to be live today, set `lifecycle_status` to `live` on the
edition yourself. The storefront still shows a published `announced` edition
as the current one, so a hub is reachable before it is live.

## 6. Turn the web flag on

`NEXT_PUBLIC_SALE_HUBS_ENABLED=true` on the web app, then deploy the web app.
The routes answer 404 while it is off.

## 7. Check it

```bash
curl -s https://api.durza.in/api/v1/sales | head -c 400
curl -s https://api.durza.in/api/v1/sales/diwali-2026 | head -c 400
curl -s https://api.durza.in/api/v1/sales/diwali-2026/diwali-2026 | head -c 400
```

The last one should list the coupons the rules resolved to.

## What is not here yet

- The ops skill `event_hub_refresh` (phase 2b) does not exist. Today a hub is
  created and published by hand in the admin, which is enough for a dated
  launch; the skill is for keeping a long-running hub fresh.
- The storefront pages for a hub are thin: the series list, the series page and
  the edition page. They show the title, the dates and the coupons; they are
  not a designed landing page yet.
