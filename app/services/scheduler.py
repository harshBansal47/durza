"""The scheduled work Couponbase runs on itself.

Replaces the host-level cron scripts. The reason is not elegance: with cron,
whether ingestion runs at all depends on somebody having configured a crontab
on a particular box, and the failure mode is silence — a coupon database that
quietly goes stale while every page still renders normally. Running the schedule
inside the process means "it stopped working" shows up in the health check and
in the job table.

Design decisions worth defending:

* **Advisory locks, held across the whole job.** With N replicas, a naive
  scheduler has every replica ingesting the same sources at the same moment.
  The lock must be *session-scoped* (`pg_try_advisory_lock`, not the `_xact_`
  variant) because a transaction-scoped lock is released the instant the
  transaction that took it commits — which would leave the actual work
  unguarded. It is explicitly unlocked in a `finally`, because SQLAlchemy
  returns the connection to the pool on close and a pool rollback does not
  release advisory locks.
* **Every attempt gets a row, failures included.** A job that records only its
  successes is indistinguishable from a job that stopped running. `skipped` is
  recorded too, so the history does not show a gap on every non-leader replica.
* **`max_instances=1` plus `coalesce=True`.** A sweep that overruns its own
  interval must not stack a second copy behind itself; the missed tick is
  dropped and the next one proceeds normally.
"""

import math
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics, sentry
from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.core.locks import try_lock as _try_lock
from app.core.locks import unlock as _unlock
from app.core.observability import get_logger
from app.models.job import JobRun, JobStatus
from app.services.alert_service import run_alert_scan
from app.services.lifecycle_service import expire_stale_coupons, refresh_coupon_prices
from app.services.verification_service import run_sweep as run_verification_sweep

logger = get_logger("couponbase.scheduler")

# Postgres advisory locks take a signed 64-bit integer. Any stable constant
# works; these just need to be distinct from each other.
_LOCK_EXPIRE = 0x0C0FFEE0
_LOCK_REFRESH = 0x0C0FFEE1
_LOCK_ALERTS = 0x0C0FFEE2
_LOCK_VERIFY = 0x0C0FFEE3
_LOCK_SCRUB_CLICKS = 0x0C0FFEE4


# A job that fails is recorded and re-raised so the caller can distinguish "ran
# and broke" from "another replica did it". The failure row is written first
# either way, so the history is never missing an outcome.
async def _expire_job(db: AsyncSession) -> str:
    expired = await expire_stale_coupons(db)
    return f"expired {expired}"


async def _refresh_job(db: AsyncSession, *, force: bool = False) -> str:
    # `force` comes from the manual "run it again" path. The sweep normally syncs
    # only sources whose own interval has elapsed, which after a provider
    # incident is exactly the wrong set to wait on.
    refreshed = await refresh_coupon_prices(db, force=force)
    return f"refreshed {refreshed} source(s)"


async def _alerts_job(db: AsyncSession) -> str:
    sent = await run_alert_scan(db)
    return f"sent {sent} alert(s)"


async def _verify_job(db: AsyncSession) -> str:
    summary = await run_verification_sweep(db)
    return (
        f"examined {summary['examined']}, {summary['attempts']} attempt(s), "
        f"{summary['retired']} retired"
    )


async def _scrub_clicks_job(db: AsyncSession) -> str:
    from app.services.click_service import scrub_visitor_hashes

    scrubbed = await scrub_visitor_hashes(db, older_than_days=get_settings().click_retention_days)
    return f"scrubbed {scrubbed} visitor hash(es)"


JOBS: dict[str, tuple[Callable[..., Awaitable[str]], int]] = {
    "expire_coupons": (_expire_job, _LOCK_EXPIRE),
    "refresh_prices": (_refresh_job, _LOCK_REFRESH),
    "send_alerts": (_alerts_job, _LOCK_ALERTS),
    "verify_coupons": (_verify_job, _LOCK_VERIFY),
    "scrub_clicks": (_scrub_clicks_job, _LOCK_SCRUB_CLICKS),
}


# ---- Failure backoff ----
#
# In-process, not in the database, and that is a deliberate choice. With N
# replicas the advisory lock already means only one of them is doing the work at
# a time, so per-process state cannot disagree with itself about *whether* a job
# is failing. It does mean a replica that starts mid-incident has no streak and
# tries once — which is a feature: a restarted process should prove the problem
# is still there rather than inherit an assumption that it isn't.

# Consecutive failures per job. Cleared by any success.
_failure_streak: dict[str, int] = {}
# `time.monotonic()` deadlines, so a clock jump (NTP step, DST on a naive host)
# cannot turn a six-hour backoff into a negative one.
_backoff_until: dict[str, float] = {}

# A streak this long is a dead-lettered job: something is structurally broken
# and retrying on a schedule will not fix it. Only the escalation changes; the
# job still retries, just very rarely, because "it stopped" and "it will never
# work" are different diagnoses and this code can only honestly claim the first.
DEAD_LETTER_AFTER = 5
# Longest pause, as a multiple of the job's own interval. 64x on a 30-minute
# alert scan is 32 hours: slow enough to stop flooding, frequent enough that
# recovery is not something an operator has to restart the process to observe.
BACKOFF_MAX_MULTIPLE = 64
# Absolute floor, for a job configured at a very short interval where
# "one interval" is still too aggressive to retry a database outage on.
BACKOFF_MIN_SECONDS = 60


def job_interval_seconds(name: str) -> float:
    """The job's configured interval, in seconds. Used as the backoff unit.

    Read from settings rather than stored on `JOBS` so the schedule stays
    defined in exactly one place — the same values that build the triggers.
    """
    settings = get_settings()
    hourly = {
        "expire_coupons": settings.expire_interval_hours,
        "refresh_prices": settings.ingestion_interval_hours,
        "verify_coupons": settings.verification_interval_hours,
        "scrub_clicks": settings.click_scrub_interval_hours,
    }
    if name in hourly:
        return hourly[name] * 3600
    return settings.alert_scan_interval_minutes * 60


def _backoff_seconds(name: str, streak: int) -> float:
    """How long to pause after `streak` consecutive failures.

    One interval after the first failure, doubling from there. The cap is a
    multiple of the interval rather than a wall-clock number, so reconfiguring
    `ALERT_SCAN_INTERVAL_MINUTES` moves the ceiling with it instead of leaving a
    stale constant behind.
    """
    interval = job_interval_seconds(name)
    return min(
        max(interval, BACKOFF_MIN_SECONDS) * (2 ** (streak - 1)),
        interval * BACKOFF_MAX_MULTIPLE,
    )


def _backoff_remaining(name: str) -> float:
    """Seconds left on the current backoff, or 0.0 if the job may run."""
    if _failure_streak.get(name, 0) <= 0:
        return 0.0
    return max(0.0, _backoff_until.get(name, 0.0) - time.monotonic())


def backoff_state() -> dict[str, dict[str, object]]:
    """Per-job backoff state, for the admin panel and the health endpoint.

    A snapshot rather than a live view, so a caller cannot be surprised by the
    dictionary changing underneath it.
    """
    snapshot: dict[str, dict[str, object]] = {}
    for name in JOBS:
        streak = _failure_streak.get(name, 0)
        remaining = _backoff_remaining(name)
        snapshot[name] = {
            "consecutive_failures": streak,
            "backoff_seconds_remaining": round(remaining, 1),
            "backing_off": remaining > 0,
            "dead_lettered": streak >= DEAD_LETTER_AFTER,
        }
    return snapshot


def _note_failure(name: str) -> int:
    """Record a failure and start (or extend) the backoff. Returns the streak."""
    streak = _failure_streak.get(name, 0) + 1
    _failure_streak[name] = streak
    _backoff_until[name] = time.monotonic() + _backoff_seconds(name, streak)
    return streak


def _note_success(name: str) -> None:
    _failure_streak.pop(name, None)
    _backoff_until.pop(name, None)


# The lock helpers live in `app.core.locks` — per-source ingestion needs the same
# two functions, and a second copy is a second chance to forget the `finally`
# that releases them. Imported under the old private names because that is what
# this module's own tests monkeypatch, and because every call site here is
# internal.


def _row(job: str, status: JobStatus, detail: str | None, started: datetime) -> JobRun:
    finished = datetime.now(UTC)
    return JobRun(
        job=job,
        status=status,
        # Truncated because an exception message can carry a whole HTML page.
        detail=detail[:2000] if detail else None,
        started_at=started,
        finished_at=finished,
        duration_ms=int((finished - started).total_seconds() * 1000),
    )


async def run_job(name: str, *, ignore_backoff: bool = False, **job_kwargs: object) -> str:
    """Execute one scheduled job under its advisory lock.

    Raises on failure so the caller can distinguish "ran and broke" from
    "another replica did it"; the failure is recorded first either way.

    `ignore_backoff` exists for the manual "run now" path. A backoff is a
    statement about *automatic* retries — it says this job has failed five times
    in a row. It is not a statement that a human looking at the failure and
    clicking "run it again" should be told no.

    `job_kwargs` is forwarded to the job body, and exists for the same reason:
    `refresh_prices` normally syncs only the sources whose own interval has
    elapsed, so a human asking it to run has to be able to say "all of them,
    now" without waiting out a degraded provider's six-hour backoff.
    """
    fn, lock_id = JOBS[name]

    if not ignore_backoff:
        remaining = _backoff_remaining(name)
        if remaining > 0:
            return await _record_skip(
                name,
                f"backoff after {_failure_streak[name]} consecutive failure(s), "
                f"{math.ceil(remaining)}s remaining",
            )

    async with AsyncSessionLocal() as db:
        if not await _try_lock(db, lock_id):
            # Losing this race is the normal state of affairs on every replica
            # that is not currently doing the work. Not an error.
            return await _record_skip(name, "another worker held the lock")

        started = datetime.now(UTC)
        try:
            detail = await fn(db, **job_kwargs)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            db.add(_row(name, JobStatus.failed, message, started))
            await db.commit()
            metrics.job_runs.labels(name, JobStatus.failed.value).inc()
            metrics.job_duration.labels(name).observe((datetime.now(UTC) - started).total_seconds())
            logger.exception("scheduled job failed", extra={"job": name})

            streak = _note_failure(name)
            pause = _backoff_seconds(name, streak)
            logger.warning(
                "job backing off after failure",
                extra={"job": name, "consecutive_failures": streak, "pause_s": round(pause, 1)},
            )
            if streak == DEAD_LETTER_AFTER:
                # Exactly once per streak, tested with `==` rather than `>=`. A
                # dead-lettered job keeps retrying on a long backoff, and
                # escalating on each of those would bury every other alert in the
                # project — which is the failure mode this mechanism was added to
                # stop. The log line and the alert are for a human to act on once.
                logger.error(
                    "job dead-lettered: still failing after %s consecutive attempts",
                    streak,
                    extra={"job": name, "last_error": message},
                )
                sentry.capture_message(
                    f"job dead-lettered after {streak} consecutive failures",
                    level="error",
                    job=name,
                )

            if streak == 1:
                # Scheduled work has no request to hang an exception off, so the
                # first failure in a streak is reported explicitly — otherwise a
                # job that has been failing for a week is invisible outside the
                # database. Later attempts in the same streak are already covered
                # by the escalation above.
                sentry.capture_exception(exc, job=name)
            raise
        else:
            db.add(_row(name, JobStatus.success, detail, started))
            await db.commit()
            elapsed = (datetime.now(UTC) - started).total_seconds()
            metrics.job_runs.labels(name, JobStatus.success.value).inc()
            metrics.job_duration.labels(name).observe(elapsed)
            # Set here rather than derived from the job_runs table at scrape
            # time: `couponza_job_last_success_timestamp_seconds` is what the
            # "the alert engine stopped" rule fires on, and it has to exist
            # without putting a query on the metrics path.
            metrics.job_last_success.labels(name).set(time.time())
            was_backing_off = _failure_streak.pop(name, 0) > 0
            _backoff_until.pop(name, None)
            logger.info(
                "scheduled job finished",
                extra={
                    "job": name,
                    "detail": detail,
                    "duration_s": round(elapsed, 2),
                    # Announced even at streak length 1: someone watching a
                    # six-hour backoff is waiting for exactly this line.
                    "recovered": was_backing_off,
                },
            )
            return detail
        finally:
            await _unlock(db, lock_id)


async def _record_skip(name: str, reason: str) -> str:
    """Record a non-attempt. Owns its own session: the caller's may be mid-flight.

    A skip is a real observation about the system — either another replica has
    the lock, or this job is in backoff — and both are invisible in the history
    without a row. Without one, a paused job and a scheduler that stopped
    entirely look identical.
    """
    async with AsyncSessionLocal() as db:
        db.add(_row(name, JobStatus.skipped, reason, datetime.now(UTC)))
        await db.commit()
    metrics.job_runs.labels(name, JobStatus.skipped.value).inc()
    logger.info("job skipped", extra={"job": name, "reason": reason})
    return "skipped"


async def _fire(name: str) -> None:
    """APScheduler entry point.

    `run_job` already records the failure and logs it with a traceback, and
    APScheduler logs unhandled job errors itself with the job id attached. A
    second try/except here would only add a bare `pass` over an already-logged
    failure.
    """
    await run_job(name)


def build_scheduler() -> AsyncIOScheduler:
    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone="UTC")
    # Start a little after boot so startup never contends with migrations.
    offset = 30

    scheduler.add_job(
        _fire,
        IntervalTrigger(hours=settings.expire_interval_hours),
        args=["expire_coupons"],
        id="expire_coupons",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(UTC) + timedelta(seconds=offset),
        replace_existing=True,
    )
    scheduler.add_job(
        _fire,
        IntervalTrigger(hours=settings.ingestion_interval_hours),
        args=["refresh_prices"],
        id="refresh_prices",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(UTC) + timedelta(seconds=offset + 30),
        replace_existing=True,
    )
    scheduler.add_job(
        _fire,
        IntervalTrigger(minutes=settings.alert_scan_interval_minutes),
        args=["send_alerts"],
        id="send_alerts",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(UTC) + timedelta(seconds=offset + 60),
        replace_existing=True,
    )
    scheduler.add_job(
        _fire,
        IntervalTrigger(hours=settings.verification_interval_hours),
        args=["verify_coupons"],
        id="verify_coupons",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(UTC) + timedelta(seconds=offset + 90),
        replace_existing=True,
    )
    scheduler.add_job(
        _fire,
        IntervalTrigger(hours=settings.click_scrub_interval_hours),
        args=["scrub_clicks"],
        id="scrub_clicks",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(UTC) + timedelta(seconds=offset + 120),
        replace_existing=True,
    )
    return scheduler


_scheduler: AsyncIOScheduler | None = None


async def start_scheduler() -> AsyncIOScheduler | None:
    global _scheduler
    settings = get_settings()
    if not settings.scheduler_enabled:
        logger.info("scheduler disabled (SCHEDULER_ENABLED is off)")
        return None
    if _scheduler is not None:
        return _scheduler

    _scheduler = build_scheduler()
    _scheduler.start()
    logger.info(
        "scheduler started",
        extra={
            "expire_every_hours": settings.expire_interval_hours,
            "ingest_every_hours": settings.ingestion_interval_hours,
            "alerts_every_minutes": settings.alert_scan_interval_minutes,
        },
    )
    return _scheduler


async def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        # wait=False: on shutdown we would rather abandon an in-flight job than
        # hang the process past the orchestrator's kill timeout.
        _scheduler.shutdown(wait=False)
        _scheduler = None


async def run_job_now(name: str, **job_kwargs: object) -> str:
    """Run a job immediately, ignoring its schedule but honouring the lock.

    Backs the admin "run now" button and gives tests a deterministic sweep
    without waiting on an interval. `job_kwargs` reaches the job body, so
    `run_job_now("refresh_prices", force=True)` is "re-ingest every source now".
    """
    if name not in JOBS:
        raise ValueError(f"unknown job: {name}")
    return await run_job(name, ignore_backoff=True, **job_kwargs)


async def last_run(db: AsyncSession, name: str) -> JobRun | None:
    result = await db.execute(
        select(JobRun)
        .where(JobRun.job == name, JobRun.status != JobStatus.skipped)
        .order_by(JobRun.started_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def recent_runs(db: AsyncSession, limit: int = 50) -> list[JobRun]:
    result = await db.execute(select(JobRun).order_by(JobRun.started_at.desc()).limit(limit))
    return list(result.scalars().all())


__all__ = [
    "JOBS",
    "backoff_state",
    "build_scheduler",
    "job_interval_seconds",
    "last_run",
    "recent_runs",
    "run_job",
    "run_job_now",
    "start_scheduler",
    "stop_scheduler",
]
