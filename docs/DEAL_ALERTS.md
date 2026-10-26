# Deal alerts (follow a store, track an offer)

## What a visitor gets

| Action | Email when | Cap |
|---|---|---|
| **Follow a store** | the store has a new live offer | one email per person per 24h |
| **Track an offer** | it is confirmed working, is about to end (within 48h), or has ended / stopped working | same email |

Everything pending is batched into one digest. Only changes from the last 7
days are reported, so a first scan after an outage never mails people about old
news. Every email has a working unsubscribe (signed link, one-click header).

## Turning it on in production

Deal alerts are sent by the scheduler job `send_deal_alerts`, and **the
scheduler is off unless `SCHEDULER_ENABLED=true`**. Switching it on starts every
job, including ingestion and verification, which contact third parties. To run
only the cheap ones:

```
SCHEDULER_ENABLED=true
SCHEDULER_JOBS=send_deal_alerts,send_alerts,expire_coupons
```

then restart the API container. The logs show `deal alert dispatched` per digest.
Jobs can later be added to `SCHEDULER_JOBS` one at a time.

## Mail

Alerts use the same SMTP settings as sign-in codes (`SMTP_*`, `EMAIL_FROM`). If
`SMTP_HOST` is unset the sender reports success without sending, so check the
log line `alert email queued (no SMTP_HOST configured)` if nothing arrives.

## How it works

`app/services/deal_alert_service.py`. State is derived from current offers on
each scan; `deal_alert_marks` remembers what a person was already told, and is
written only after a channel accepted the send, so failed deliveries are retried
(after a 2h back-off). History lands in `alert_events` with `batch_id` shared by
all channel rows of one alert.
