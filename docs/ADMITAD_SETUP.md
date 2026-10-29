# Admitad publisher integration

The backend fetches Admitad coupons for a publisher website and imports reported
actions into conversion and revenue records. The storefront does not need
Admitad-specific changes: affiliate URLs remain behind the existing `/go`
redirect routes.

## Environment

Set these on the deploy host's `.env` **before** deploying (a container reads
`.env` only when it is created):

```dotenv
ADMITAD_CLIENT_ID=
ADMITAD_CLIENT_SECRET=
ADMITAD_WEBSITE_ID=      # ad space id, from GET /websites/
SCHEDULER_ENABLED=true
```

The API client requests its OAuth access token using the client id and secret; do
not put either value, or a derived Basic authorization header, in a database row.
The derived `ADMITAD_BASE_64_HEADER` is not needed.

## Database rows are created by the deploy

Admitad is declared in `app/ingestion/provider_config.py`. Every deploy runs
`python -m app.tools.sync_sources`, which creates (or refreshes) the `Source` and
the `AffiliateProvider` (slug `admitad`) from that file and the environment. There
is nothing to seed by hand. To preview or re-run it manually:

```bash
podman-compose run --rm --no-deps -T app python -m app.tools.sync_sources --dry-run
```

To switch the provider off, set `enabled=False` in `provider_config.py` and deploy.

## Coupon source

Create an enabled `Source` with `kind=admitad`, a unique name and slug, and
`credential_ref=ADMITAD_CLIENT_ID,ADMITAD_CLIENT_SECRET`. Its `config` needs the
publisher website ID returned by `GET /websites/`:

```json
{
  "website_id": "YOUR_WEBSITE_ID",
  "page_size": 100
}
```

`page_size` is optional and must be between 1 and 500. Set
`SCHEDULER_ENABLED=true`; if `SCHEDULER_JOBS` is explicitly set, include
`refresh_prices` and `sync_conversions` for both flows to run automatically.
The adapter uses the OAuth token endpoint and the coupons endpoint, follows
offset pagination, and stores only tracking destinations supplied by Admitad.
A store with no affiliate network that receives an Admitad offer is set to `admitad`
automatically, so its outbound links carry the click reference as Admitad's `subid`.
A store already on another network keeps it. (Merchant matching is by domain first, so
an offer for `nykaa.com` attaches to your existing Nykaa store.)

## Conversion sync

The scheduled `sync_conversions` job uses the same client ID and secret when
both are configured. It reads actions from `GET /statistics/actions/`, normalizes
their transaction IDs, status, commission, order amount, dates, and click
sub-ID, then upserts conversions idempotently. Its frequency and lookback are
controlled by `CONVERSION_SYNC_INTERVAL_HOURS` and
`CONVERSION_SYNC_LOOKBACK_DAYS`.

For revenue rows to be associated with an affiliate provider, create an
`AffiliateProvider` whose slug is `admitad`.

## Before the first run

1. **Verify the ad space.** `GET /websites/` shows `validation_passed`. The storefront serves the
   `mitgo-verification` meta tag on the homepage; in Mitgo ID the verification page must be
   completed with **Finish**, and the check can take up to 15 minutes.
2. **Join advertiser programmes.** Coupons, links and commissions are returned only for programmes
   the website is connected to and approved for.
3. **Token scope.** The client asks for `advcampaigns websites coupons coupons_for_website
   statistics deeplink_generator`. `coupons_for_website` is required by the per-website coupons
   endpoint; without it Admitad answers `insufficient_scope`.
4. **Scheduler.** `SCHEDULER_ENABLED=true` and, if `SCHEDULER_JOBS` is set, include `refresh_prices`
   and `sync_conversions`.

## Notes on the data

* The statistics API takes dates as `dd.mm.yyyy`; the client formats them that way.
* The order total is `cart` and the commission is `payment`. The click reference comes back in
  `subid` (the parameter the redirect appends).
* `approved_but_stalled` is treated as pending: approved, but the payout is held.
* Campaign names such as `Acer [CPS] IN` are cleaned to `Acer` before a store is created.
* Discount text such as `2 000 ₹` is parsed into a fixed amount of 2000.
* The statistics field names above come from Admitad's documentation. There were no
  conversions to inspect when this was written, so check one real row after the first sale.
