# Logging, stats and retention

Everything the API logs goes to **stdout** (JSON when `LOG_FORMAT=json`). On top of
that, an optional **stored log** keeps a searchable copy in the database that
deletes itself.

## What is logged

**Every API endpoint** is covered by the request line (`durza.access`): method,
route template, status, duration and request id. Beyond that:

- `user_id` and `user_role` are added whenever the caller was signed in, so "who
  did this" is answerable for every endpoint without touching any of them.
- For anything that is not a GET (create, edit, delete, verify, save, track...),
  the concrete ids from the path are added as `path_params`, so a line reads
  `PATCH /coupons/{coupon_id}` *and* says which coupon.
- A `401`, `403` or `429` logs at WARNING, so refused requests are stored and
  countable. Slow requests (`SLOW_REQUEST_SECONDS`) log at WARNING; 5xx at ERROR.
- Health and metrics probes are skipped.

**Operations** (what the endpoints and jobs do behind the scenes):

| Area | Logger | What |
|---|---|---|
| Public sign-in | `durza.auth` | code requested (only when one was actually sent), code rejected, sign-in succeeded, Google rejections, refresh rejections. When no code is sent the API answers 503 without saying why, so the reason is logged instead: hourly address cap (WARNING), resend cooldown (INFO), delivery failure (ERROR) |
| Back office | `durza.admin` | sign-in, failed sign-in, sign-out, every create/edit/delete, who triggered an ingestion (also stamped on the run row, so the dashboard's Trigger column shows `api:<user id>` or `admin-ui:<user id>`, not `scheduler`) |
| Ingestion runs | `durza.lifecycle` | success at INFO; a failed or partial run now logs **WARNING with the reason** (it used to log as "ingested" and keep the reason only on the run row) |
| Providers | `durza.ingestion.*` | each Admitad and Awin page fetched, failed page requests, every retry |
| Email | `durza.email` | sent / queued / failed, with `email_fp` and never the address |
| Affiliate routing | `durza.routing` | which source a click went to and why, at DEBUG (one line per click is too many for INFO) |
| Health | `durza.system` | a job health check that errored instead of answering |
| Scheduler, alerts, verification, conversions | existing | already present |

## Privacy

- Addresses are never logged by the app: sign-in and email events carry
  `email_fp`, a 12-character hash, so a log can show "the same address tried five
  times" without saying whose. (An earlier version of `email.py` logged recipients
  in plain text; that is fixed.)
- As a safety net, the stored log replaces any email address with
  `[email:<fingerprint>]` in the message, the fields and the traceback, whoever
  logged it. SMTP errors, which echo the recipient, are the usual way one gets in.
- Fields named like `password`, `token`, `secret`, `authorization`, `cookie` or
  `credential` are redacted before storage. Admin audit lines record field
  *names* that changed, never values.
- This does not scrub **stdout**: a third-party library that writes an address to
  its own logger would still show it there. Keep container logs short-lived.

Request lines are INFO, so `LOG_LEVEL=WARNING` silences them on stdout if they are
too chatty.

## The stored log

Off by default. Turn it on in the server's `.env`:

```
LOG_STORE_ENABLED=true
LOG_RETENTION_DAYS=14      # optional, default 14
```

- Stored: the app's own loggers from `LOG_STORE_MIN_LEVEL` (default INFO); the
  access log and third-party loggers only from WARNING. Normal page views are
  **not** stored, because Prometheus metrics already count them.
- A burst of the *same* line is throttled: after 30 identical rows in a minute the
  rest are counted and one `suppressed N repeats of: ...` row is written, so a bot
  hammering one endpoint (or a provider failing in a loop) cannot fill the table.
- Rows are written in batches every ~5 seconds from memory. If the database is
  unreachable the batch is dropped, never retried forever, and the request path is
  unaffected.
- The `scrub_logs` job deletes rows older than `LOG_RETENTION_DAYS`, every
  `LOG_SCRUB_INTERVAL_HOURS` (default 6), in slices of 5,000.
  **If you set `SCHEDULER_JOBS`, add `scrub_logs` to the list**, or nothing deletes
  the rows.

## Reading it

- Admin: **App Logs** in the sidebar (admins only, read-only).
- API (admin token): `GET /api/v1/admin/logs/stats?hours=24` gives counts by level
  and logger, an hourly series and the latest warnings/errors.
  `GET /api/v1/admin/logs?min_level=WARNING&logger=durza.auth&q=rejected` searches.

## Raw container logs

These are not touched by the above and are rotated by the container runtime. On the
VM, cap them so they cannot fill the disk, in `/etc/containers/containers.conf`:

```
[containers]
log_size_max = 10485760   # 10 MiB per container
```
