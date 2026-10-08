"""System endpoints: liveness, readiness, metrics.

`/health` and `/health/deep` exist as two separate endpoints rather than one
with a query parameter, because they are asked by different things with
different costs. A load balancer polls liveness thousands of times a minute and
must not be able to turn a slow database into a restart storm. A deploy pipeline
asks the deep check once and genuinely needs to know about the database.

The deep check reports every dependency independently rather than failing fast,
so "is it the database or the scheduler?" is answerable from the response
instead of from a log dive.
"""

import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.config import get_settings
from app.core.database import get_db
from app.core.observability import get_logger
from app.models.job import JobStatus
from app.services.scheduler import backoff_state, last_run

router = APIRouter(tags=["system"])
logger = get_logger("couponza.health")

# A job that has not run within this many multiples of its interval is stale.
# The multipliers differ because the jobs have wildly different cadences — a
# failure to expire coupons for 3 hours is not the same problem as a failure to
# send alerts for 3 hours.
_STALE_AFTER = {
    "expire_coupons": timedelta(hours=6),
    "refresh_prices": timedelta(hours=48),
    "send_alerts": timedelta(hours=3),
    # Generous: verification is a background quality signal, not a
    # user-visible promise, and a missed sweep costs nothing for a day.
    "verify_coupons": timedelta(days=2),
    # Daily privacy housekeeping; a missed day is harmless, a missed week is not.
    "scrub_clicks": timedelta(days=3),
}


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness. Cheap, uncached, touches nothing external.

    If this fails the process is broken and should be restarted; it deliberately
    does not consult the database, because a database blip must not trigger a
    restart of every healthy replica.
    """
    return {"status": "ok", "environment": get_settings().environment}


@router.get("/health/deep")
async def health_deep(response: Response, db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    """Readiness. Checks the database and reports scheduler freshness.

    Returns 503 when a dependency is down so orchestrators actually stop routing
    traffic here, but the body always describes *everything* it checked — a
    partial failure report is worse than a failure, because it looks like a pass.
    """
    checks: dict[str, object] = {}
    healthy = True

    started = time.perf_counter()
    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = {"status": "ok", "latency_ms": _ms(started)}
    except Exception as exc:  # noqa: BLE001
        # Deliberately broad: this is the check that is supposed to report
        # whatever went wrong, so narrowing it to SQLAlchemyError would let an
        # asyncpg or DNS failure escape and turn a "degraded" answer into a 500.
        healthy = False
        checks["database"] = {"status": "error", "error": type(exc).__name__}
        logger.warning("database health check failed", extra={"error": str(exc)})

    for job, limit in _STALE_AFTER.items():
        # These hit the same database that just failed. Reporting "unknown"
        # instead of raising is the entire point of a check endpoint: it has to
        # stay answerable precisely when things are broken.
        try:
            report = await _job_health(db, job, limit)
        except Exception:  # noqa: BLE001
            report = {"status": "unknown"}

        # The in-process backoff is reported alongside the durable history, and
        # not merged into it. The history says the last attempt failed; only this
        # says the job is currently *paused*, which is the difference between a
        # bug being retried and a bug being handled. Neither alone is enough:
        # without the history a live backoff looks fine, and without the
        # backoff a failed row looks like the job is still hammering away.
        entry = backoff_state().get(job, {})
        if entry.get("dead_lettered"):
            report["dead_lettered"] = True
            report["consecutive_failures"] = entry["consecutive_failures"]
        elif entry.get("backing_off"):
            report["backing_off"] = True
            report["backoff_seconds_remaining"] = entry["backoff_seconds_remaining"]
        checks[job] = report

    checks["email"] = {
        "status": "configured" if get_settings().email_configured else "not configured"
    }
    checks["push"] = {
        "status": "configured" if get_settings().push_configured else "not configured"
    }

    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if healthy else "degraded", "checks": checks}


async def _job_health(db: AsyncSession, job: str, limit: timedelta) -> dict[str, object]:
    """Report one scheduled job's freshness.

    A job that has never run is *not* reported as stale. The scheduler is off by
    default, so "no history" is the correct and expected state in every local
    environment and in CI — calling that unhealthy would train everyone to
    ignore this endpoint.
    """
    row = await last_run(db, job)
    if row is None:
        return {"status": "never run"}

    # The column is `DateTime(timezone=True)`, but some drivers (SQLite among
    # them) hand back a naive datetime anyway. Normalising here is what keeps the
    # endpoint answering on every database rather than only on Postgres.
    started = row.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    age = datetime.now(UTC) - started
    if row.status is JobStatus.failed:
        # Not "unhealthy" — a single failed tick with a fresh successful run
        # after it is normal. Report it and let the operator decide.
        return {"status": "last run failed", "detail": row.detail, "at": started.isoformat()}
    if age > limit:
        return {
            "status": "stale",
            "age_s": int(age.total_seconds()),
            "at": started.isoformat(),
        }
    return {"status": "ok", "at": started.isoformat(), "detail": row.detail}


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


@router.get("/metrics", include_in_schema=False)
async def prometheus_metrics() -> Response:
    """Prometheus scrape endpoint.

    Gated on `METRICS_ENABLED` because the counters are process-global and this
    endpoint should not be reachable on a laptop that happens to be running the
    API. `raise_in_schema=False` because it is for machines, not humans.
    """
    settings = get_settings()
    if not settings.metrics_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="metrics are disabled")

    # Deliberately does not compute the stale-coupon gauge here: that is a table
    # scan, and doing it at scrape resolution is how your own monitoring takes
    # down the database. The gauge is set by the expiry job, which already has
    # those rows in hand for free.
    payload, content_type = metrics.render()
    return Response(content=payload, media_type=content_type)
