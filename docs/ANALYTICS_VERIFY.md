# Analytics verification runbook

## Preflight
```bash
curl -i -X OPTIONS https://api.durza.in/api/v1/analytics/event \
  -H 'Origin: https://www.durza.in' \
  -H 'Access-Control-Request-Method: POST' \
  -H 'Access-Control-Request-Headers: content-type,x-session-id'
```
Expect `access-control-allow-credentials: true` and the Durza origin in `access-control-allow-origin`.

## Browser cookie checks
- Accept analytics; confirm `cz_sid` is set for `api.durza.in`.
- Decline analytics; confirm events stop and `cz_vid`/`cz_sid` are cleared.
- Taste signals remain in `localStorage`; they are not uploaded.

## SQL counts
```sql
select count(*) from analytics_sessions;
select count(*) from analytics_events;
select count(*) from coupon_impressions;
```

## Staff analytics dashboard

With the frontend and API running, open `http://localhost:3000/admin/analytics`
and sign in with an `admin`, `editor`, or `analytics_viewer` account. The page
calls the staff-only endpoint through the frontend server:

```text
GET /api/v1/admin/analytics/overview?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
```

Both date boundaries are included and interpreted as UTC. The maximum query
window is 366 days. Operational health counters cover the rolling seven days,
regardless of the selected reporting range. Coupon CTR is clicks divided by
impressions, expressed as a percentage. Verification counts are automated
checker outcomes; inconclusive checks are not counted as either success or
failure. Revenue values are grouped by their stored currency and are not
converted or summed across currencies.

The endpoint reads daily rollups for traffic, coupon, merchant, and affiliate
performance, and raw session/event/verification records for their respective
counts. Empty rollups are shown as zero/no data; they do not prove that the
tracking or rollup pipeline is healthy. CSV export contains summary metrics,
is available only to admins, and is enforced by the API as well as the UI. No
raw visitor identifiers are included.
