# vCommission campaign catalogue

vCommission campaigns are stored separately from coupons. Synchronization does
not create coupon codes, discount claims, or stores. Campaign payout information
is affiliate compensation and must not be presented as a shopper discount.

## Configure and run

Set the campaigns feed URL in the backend environment:

```dotenv
VCOMMISSIONS_CAMPAIGNS_URL=https://provider.example/campaigns?credential=...
VCOMMISSIONS_SYNC_INTERVAL_HOURS=12
SCHEDULER_ENABLED=true
```

Treat the complete URL as a credential if the provider places a token in its
query string. Do not expose it in browser configuration, logs, screenshots, or
support tickets. The importer accepts the provider's JSON envelope with
`success: true` and `data.campaigns`; each campaign needs an `id`, `title`, and
HTTP(S) `tracking_link`. Optional campaign attributes are normalized when
present. The provider feed returns all campaigns in one request; no page parameter is
added. When `data.count` is supplied, the importer verifies it equals the
number of returned campaign records before applying any changes. A mismatch
fails safely so an incomplete response cannot deactivate campaigns.

Apply the database migration before enabling synchronization. The scheduler
runs `sync_vcommission_campaigns` at the configured interval. If
`SCHEDULER_JOBS` is explicitly set, include that job name. Admins can also
trigger a sync with `POST /api/v1/vcommission/admin/sync` and inspect the latest
attempt with `GET /api/v1/vcommission/admin/sync/latest`.

## Public API

* `GET /api/v1/vcommission/campaigns` returns active campaigns, with optional
  `search`, `category`, and `country` filters and `skip`/`limit` pagination.
* `GET /api/v1/vcommission/campaigns/{id}` returns one active campaign.
* `GET /api/v1/vcommission/campaigns/{id}/go` records a click and redirects to
  the tracking URL supplied by vCommission.

Public campaign responses deliberately omit the upstream tracking URL. The
redirect preserves that URL exactly: this integration does not append a
click-reference parameter until vCommission documents the correct mechanism.
The click is still recorded internally, but conversion attribution is not
implemented by this campaign-feed integration.

An empty or partially invalid feed never deactivates campaigns. A complete,
non-empty feed deactivates campaigns absent from that feed but retains their
records for auditability. Existing stores are matched by normalized domain or
name; unmatched campaigns remain unassociated.
