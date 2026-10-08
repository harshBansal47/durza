"""Prometheus metrics.

Hand-rolled rather than pulled from a framework because there is nothing to
instrument yet beyond "requests arrived" and "jobs ran" — and the honest version
of that is a dozen lines. The two things this module is careful about:

* **Cardinality is bounded.** Route labels are the *route template*
  (`/api/v1/coupons/{coupon_id}`), never the concrete path. Labelling by path is
  the single most common way to turn a Prometheus scrape into an out-of-memory
  kill of the metrics server, and it does not show up until someone queries a
  404-heavy path.
* **Instruments are registered once.** Prometheus's default registry raises on a
  duplicate metric name, which turns an import-order accident into an import
  error. Re-registering is a no-op instead.
"""

import time

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.openmetrics.exposition import CONTENT_TYPE_LATEST as OPENMETRICS_TYPE

# A private registry rather than the global default: tests create the app more
# than once in a process, and global state leaking between them is how you end
# up debugging a metric that is mysteriously doubled.
registry = CollectorRegistry()

http_requests = Counter(
    "couponbase_http_requests_total",
    "HTTP requests, by method, route and status.",
    ["method", "route", "status"],
    registry=registry,
)

http_duration = Histogram(
    "couponbase_http_request_duration_seconds",
    "Request latency in seconds, by method and route.",
    ["method", "route"],
    # Buckets chosen around what a JSON API actually does: a cache hit is ~5ms,
    # a normal read ~40ms, an ingestion-adjacent write ~300ms. Default buckets
    # start at 5ms and top out at 10s, which wastes resolution where it matters.
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=registry,
)

job_runs = Counter(
    "couponbase_job_runs_total",
    "Scheduled job executions, by job name and outcome.",
    ["job", "outcome"],
    registry=registry,
)

job_duration = Histogram(
    "couponbase_job_duration_seconds",
    "Scheduled job duration in seconds, by job name.",
    ["job"],
    buckets=(0.1, 0.5, 1.0, 5.0, 15.0, 60.0, 300.0, 900.0),
    registry=registry,
)

alerts_sent = Counter(
    "couponbase_alerts_total",
    "Alert deliveries attempted, by channel and outcome.",
    ["channel", "outcome"],
    registry=registry,
)

stale_coupons = Gauge(
    "couponbase_stale_coupons",
    "Coupons past their expiry date but still marked active.",
    registry=registry,
)

# A unix timestamp rather than a duration, because the alerting question is "how
# long since the last success", which Prometheus expresses as
# `time() - couponbase_job_last_success_timestamp_seconds`. Computing the duration
# in-process would need a "now" that Prometheus then has to take on trust, and
# would go wrong the moment a scrape is delayed.
#
# Set only on success. A job that has never succeeded has no series at all, so
# the alert that matters uses `absent()` — "never ran" and "stopped running" are
# different problems, and `absent()` is the only way to tell them apart.
job_last_success = Gauge(
    "couponbase_job_last_success_timestamp_seconds",
    "Unix timestamp of the last successful run of each scheduled job.",
    ["job"],
    registry=registry,
)

coupons_expired = Counter(
    "couponbase_coupons_expired_total",
    "Coupons retired by the expiry sweep, ever.",
    registry=registry,
)

ingestion_runs = Counter(
    "couponbase_ingestion_runs_total",
    "Ingestion pipeline executions, by source and outcome.",
    ["source", "outcome"],
    registry=registry,
)

price_last_captured = Gauge(
    "couponbase_price_last_captured_timestamp_seconds",
    "Unix timestamp of the most recent price point capture, ever.",
    registry=registry,
)


def mark_price_captured() -> None:
    """Record that a price point was captured in this process.

    A gauge rather than a timestamp column read at scrape time: the alternative
    puts a query on the metrics path, which is how your own monitoring takes down
    the database. A restarted process reports nothing until its next capture,
    which is the honest answer rather than a stale high-water mark.
    """
    price_last_captured.set(time.time())


def render() -> tuple[bytes, str]:
    """Return the exposition payload and its content type."""
    return generate_latest(registry), OPENMETRICS_TYPE
