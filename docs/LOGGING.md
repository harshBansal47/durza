# Logging, stats and retention

Everything the API logs goes to **stdout** (JSON when `LOG_FORMAT=json`). On top of
that, an optional **stored log** keeps a searchable copy in the database that
deletes itself.

## What is logged

| Where | Logger | Level | What |
|---|---|---|---|
| Every HTTP request | `durza.access` | INFO (WARNING if slow, ERROR if 5xx) | method, route template, status, duration, request id |
| Public sign-in | `durza.auth` | INFO / WARNING | code requested, code rejected, sign-in succeeded, Google rejections, refresh rejections |
| Back-office | `durza.admin` | INFO / WARNING | sign-in, sign-out, failed sign-in, every create/edit/delete, who triggered an ingestion |
| Providers | `durza.ingestion*` | INFO / WARNING | each Admitad page fetched, each retry |
| Scheduler, ingestion, lifecycle | `durza.scheduler`, `durza.lifecycle`, pipeline | INFO+ | already present |

Emails are never logged: sign-in events carry `email_fp`, a 12-character hash, so a
log can show "the same address tried five times" without saying whose. IPs appear
only as the existing `ip_hash`. Fields named like `password`, `token`, `secret`,
`authorization`, `cookie` or `credential` are redacted before storage.

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
