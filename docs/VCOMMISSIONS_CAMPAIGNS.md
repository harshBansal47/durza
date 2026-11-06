# vCommission campaign catalogue

vCommission campaigns are stored separately from coupons. Synchronization does
not create coupon codes, discount claims, or stores. Campaign payout information
is affiliate compensation and must not be presented as a shopper discount.

## Configure and run

Set the campaigns feed URL in the backend environment:

```dotenv
VCOMMISSIONS_CAMPAIGNS_URL=https://provider.example/campaigns?credential=...
SCHEDULER_ENABLED=false
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

Apply the database migration before the first sync. This integration can be
operated manually without starting the scheduler: keep `SCHEDULER_ENABLED=false`
and have an admin call `POST /api/v1/vcommission/admin/sync` whenever the feed
should be refreshed. Check the latest attempt with
`GET /api/v1/vcommission/admin/sync/latest`. The sync uses the same advisory
lock as scheduled work, so it is safe if a scheduled instance is running
elsewhere.

The campaign catalogue page is `/affiliate-campaigns`. It reads the public
campaign API and sends visitors through the backend's tracked campaign redirect;
no provider credential is needed in the frontend.

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

## Parsed advertiser terms

Each campaign's `description` is advertiser-written HTML that follows one template. At import
time `app/services/vcommission_terms.py` reads it into `affiliate_campaigns.terms` (JSON) and
sets `needs_review`. It must run on the raw HTML: the stored `description` has been flattened
to one line and cannot be parsed afterwards, so campaigns imported before this column existed
get their terms on the next sync.

`terms` holds, per campaign:

- `channels`: the advertiser's Promotion Methods matrix (`telegram`, `coupon`, `email`, `sms`,
  `cashback`, `search`, ...), each `allowed`, `conditional` (with the advertiser's note),
  `not_allowed` or `unavailable`;
- `cookie_days`, `validation_days`, `conversion_type`, `geo`;
- `rules`: the Detailed KPI list, each tagged (`cancelled_not_payable`, `brand_bidding_banned`,
  `other_affiliate_attribution`, `approved_discount_code`, `code_payout_rule`, `geo`, `other`);
- `payout_structure` and `payout_effective_from`: anything other than `api_only` means the
  advertiser's text describes slabs, user types or categories that the API's single payout
  number does not capture, so do not rank on that number alone;
- `review_reasons`.

`needs_review` is true when the channel matrix or KPI section is missing, when a rule says which
discount codes affect payout, or when a payout change is dated in the future. That is the queue
a person should clear (`GET /vcommission/campaigns?needs_review=true`, admin only).

Use `can_promote(terms, channel)` before posting or showing anything for a campaign. It fails
closed: missing or unrecognised terms, "Not Allowed", and "Not Available" are all a no, and a
conditional ("Allowed (Only generic)") is a no unless the caller accepts the condition.

`terms` contains advertiser wording and is only returned by the admin-only endpoints. Shopper
text should be written in our own words, never copied from it.
