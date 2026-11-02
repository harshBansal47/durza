# Saved-item email updates

## What a visitor gets

| Action | Email when | Cap |
|---|---|---|
| **Follow a store** | the store has a new live offer | one digest per scheduler scan |
| **Save a coupon** | it is confirmed working, enters the 48-hour expiry window, or has ended / stopped working | same email |

Everything pending is batched into one digest. Only changes from the last scan
interval are reported, so old changes are not replayed. Alert contents and
per-user alert history are not stored. Every email has a working unsubscribe
(signed link, one-click header).

## Turning it on in production

Deal alerts are sent by the scheduler job `send_deal_alerts`, and **the
scheduler is off unless `SCHEDULER_ENABLED=true`**. Switching it on starts every
job, including ingestion and verification, which contact third parties. To run
only the cheap ones:

```
SCHEDULER_ENABLED=true
SCHEDULER_JOBS=send_deal_alerts,expire_coupons
```

then restart the API container. The logs show `deal alert dispatched` per digest.
Jobs can later be added to `SCHEDULER_JOBS` one at a time.

## Mail

Alerts use the same SMTP settings as sign-in codes (`SMTP_*`, `EMAIL_FROM`). If
`SMTP_HOST` is unset the sender reports success without sending, so check the
log line `alert email queued (no SMTP_HOST configured)` if nothing arrives.

## How it works

`app/services/deal_alert_service.py`. Each scan derives events from saved-item
timestamps and current offer state. No per-user alert rows are written.
Because there is no delivery history, a failed send is not queued for a later
retry.
