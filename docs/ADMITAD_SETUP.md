# Admitad publisher integration

The backend fetches Admitad coupons for a publisher website and imports reported
actions into conversion and revenue records. The storefront does not need
Admitad-specific changes: affiliate URLs remain behind the existing `/go`
redirect routes.

## Environment

Set these backend environment variables:

```dotenv
ADMITAD_CLIENT_ID=
ADMITAD_CLIENT_SECRET=
```

The API client requests its OAuth access token using these values; do not put
either value, or a derived Basic authorization header, in a database row. The
derived `ADMITAD_BASE_64_HEADER` is not needed.

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
Set each relevant merchant's `affiliate_network` to `admitad` so the backend
adds its click reference as Admitad's `subid` on the outbound redirect.

## Conversion sync

The scheduled `sync_conversions` job uses the same client ID and secret when
both are configured. It reads actions from `GET /statistics/actions/`, normalizes
their transaction IDs, status, commission, order amount, dates, and click
sub-ID, then upserts conversions idempotently. Its frequency and lookback are
controlled by `CONVERSION_SYNC_INTERVAL_HOURS` and
`CONVERSION_SYNC_LOOKBACK_DAYS`.

For revenue rows to be associated with an affiliate provider, create an
`AffiliateProvider` whose slug is `admitad`.
