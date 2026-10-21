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
