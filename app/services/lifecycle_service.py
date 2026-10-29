"""Coupon lifecycle work the scheduler owns.

Thin service layer over `app.ingestion` so the scheduler doesn't need to know
that ingestion has a pipeline, adapters and a run ledger — and so the same
functions are callable from an admin "run now" button without reaching into
the ingestion package.

The two jobs:

* **Expire** is pure bookkeeping. Cheap, safe to run hourly, and it is the
  difference between a shopper seeing "No longer valid" and a shopper being
  sent to a dead code.
* **Refresh** re-ingests sources, each on its own schedule. This is the
  expensive one, and it is deliberately the only place that decides *which*
  sources are due — see `refresh_coupon_prices`.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.database import AsyncSessionLocal
from app.core.locks import source_lock_id, try_lock, unlock
from app.core.timezones import ensure_utc
from app.ingestion.credentials import credentials_satisfied
from app.ingestion.lifecycle import expire_stale_coupons as _expire_stale_coupons
from app.ingestion.pipeline import run_ingestion
from app.ingestion.source_health import effective_interval_minutes
from app.models.coupon import Coupon, CouponStatus
from app.models.ingestion_run import IngestionRun, RunStatus, SyncMode
from app.models.source import Source

logger = logging.getLogger("durza.lifecycle")

#: How many sources one sweep ingests at the same time.
#:
#: The sweep itself is already serialised across replicas by the scheduler's
#: job lock, so this is not about mutual exclusion — it is about not making four
#: healthy providers wait behind one slow HTTP call. Small on purpose: a wider
#: fan-out mostly buys concurrent connections against the same database and
#: concurrent connections to the same provider, and providers rate-limit.
MAX_CONCURRENT_SOURCES = 4


async def expire_stale_coupons(db: AsyncSession) -> int:
    expired = await _expire_stale_coupons(db)
    await _publish_stale_gauge(db)
    if expired:
        # Adding zero to a counter is harmless but shows up in the series as a
        # no-op event, and a sweep that retires nothing is not an event.
        metrics.coupons_expired.inc(expired)
    return expired


async def _publish_stale_gauge(db: AsyncSession) -> None:
    """Publish how many coupons are past their expiry but still marked active.

    A non-zero value here that keeps growing means the expiry job has stopped
    working while the site keeps serving dead codes — which is the exact failure
    this whole subsystem exists to make visible.

    Set here rather than in the /metrics handler because the expiry job has
    already issued this query; re-running it per scrape would put a table scan
    on the metrics path.
    """
    still_active = await db.execute(
        select(func.count())
        .select_from(Coupon)
        .where(
            Coupon.status == CouponStatus.active,
            Coupon.expires_at.is_not(None),
            Coupon.expires_at <= datetime.now(UTC),
        )
    )
    metrics.stale_coupons.set(int(still_active.scalar_one()))


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


def is_source_due(source: Source, *, now: datetime | None = None) -> bool:
    """Whether this source has waited out its own interval.

    The interval is `source_health.effective_interval_minutes`, not the raw
    `sync_interval_minutes`: a provider that is degrading gets polled less often
    on purpose, and asking this function for the raw interval would throw that
    away and put the whole catalogue on one provider's failure schedule.

    A source that has never run is always due. Reading it the other way — "no
    `last_sync_at`, so nothing is waiting on a timer" — is how a newly added
    provider sits idle until somebody notices the empty row.

    Deliberately a Python function over already-loaded rows rather than a `CASE`
    in SQL. There are tens of sources, and putting the interval arithmetic in the
    database would mean a second implementation of the multiplier in
    `source_health`, free to disagree with the first. That trade only becomes
    attractive at tens of thousands of sources.
    """
    if source.last_sync_at is None:
        return True
    waited = (now or datetime.now(UTC)) - ensure_utc(source.last_sync_at)
    return waited >= timedelta(minutes=effective_interval_minutes(source))


def _supports_concurrent_sessions(db: AsyncSession) -> bool:
    """Whether this database can safely run several sources at once.

    Postgres can: one pool connection per session, independent transactions.
    The test suite's in-memory SQLite cannot. It is served by a single shared
    connection (`StaticPool`), so two concurrent sessions interleave statements
    on that one connection and two transactions quietly become one — measured
    loss rather than a crash: six concurrent inserts commit three rows.

    So this is a correctness guard, not a test accommodation. It is also cheap to
    state, which matters more than the alternative: a sweep that looks concurrent
    and is not would be the worse failure.
    """
    bind = db.bind
    return bind is not None and bind.dialect.name == "postgresql"


@dataclass(frozen=True)
class _Target:
    """What the sweep needs to remember about one source once its row is stale.

    Plain values rather than the ORM object, because the sweep closes its read
    transaction before running anything and a rollback expires every attribute of
    every object in the session. Reading `source.slug` afterwards is not a style
    question: it either raises `MissingGreenlet` trying to refresh asynchronously,
    or on a detached instance it quietly returns a value from a transaction that
    no longer exists.
    """

    source_id: Any
    slug: str
    #: Whether this source has what it needs to be fetched. Only ever used to
    #: choose a log message, never to skip the run — the pipeline's own refusal
    #: is the thing an operator needs to see recorded.
    configured: bool


async def refresh_coupon_prices(
    db: AsyncSession, *, force: bool = False, source_slugs: list[str] | None = None
) -> int:
    """Re-ingest every source that is due. Returns the number of successful runs.

    Each source is independent, in four separate senses:

    * **Independent failure.** A source that raises is logged and the sweep
      carries on, so one dead affiliate feed cannot stop the other three from
      updating. This is why each source gets its own session: sharing one leaves
      it in an aborted transaction after the first failure, and every later
      source then fails for a reason that has nothing to do with it.
    * **Independent schedule.** Due-ness is decided per source from that source's
      own interval and health, so the sweep can tick every six hours while a
      healthy provider syncs hourly and a degraded one waits out its multiplier.
      `force=True` is the operator's override and ignores all of it.
    * **Independent concurrency.** Sources are fetched over HTTP and nothing about
      one source's data is needed to start another's, so they overlap up to
      `MAX_CONCURRENT_SOURCES` — see that constant for why the fan-out is small.
    * **Independent lock.** Each run takes its own source's advisory lock rather
      than the sweep's, which is what will make a future "run just this source"
      button safe without any of the above being re-derived.

    `source_slugs` narrows the sweep to those providers: the admin's "Ingest now"
    button passes the ones the operator selected. It is a filter, not an override;
    a disabled source is still excluded.

    Disabled sources are excluded here rather than in the pipeline: `is_enabled`
    is a human's decision not to use a provider, and sweeping it every tick only
    to have the pipeline refuse it would fill the run ledger with rows saying
    nothing except that the scheduler is running.
    """
    stmt = select(Source).where(Source.is_enabled.is_(True)).order_by(Source.slug)
    if source_slugs is not None:
        stmt = stmt.where(Source.slug.in_(source_slugs))
    result = await db.execute(stmt)
    enabled = list(result.scalars().all())
    now = datetime.now(UTC)
    targets = [
        _Target(
            source_id=source.id,
            slug=source.slug,
            configured=credentials_satisfied(source.kind.value, source.credential_ref),
        )
        for source in enabled
        if force or is_source_due(source, now=now)
    ]

    # The read transaction is closed before a single source runs. Left open it
    # would park a connection in an idle-in-transaction state for the length of
    # the sweep, blocking vacuum from cleaning up rows the sweep itself just
    # wrote, and on the test database it would put a second session on top of the
    # connection the first one is still using.
    await db.rollback()

    logger.info(
        "source refresh sweep starting",
        extra={
            "enabled": len(enabled),
            "due": len(targets),
            "not_due": len(enabled) - len(targets),
            "forced": force,
        },
    )
    if not targets:
        return 0

    results: list[IngestionRun | BaseException | None]
    if _supports_concurrent_sessions(db):
        results = await asyncio.gather(
            *(_run_target(target) for target in targets),
            return_exceptions=True,
        )
    else:
        results = []
        for target in targets:
            try:
                results.append(await _run_target(target))
            except Exception as exc:  # noqa: BLE001 - reported by _summarise
                results.append(exc)

    return _summarise(targets, results)


async def _run_target(target: _Target) -> IngestionRun | None:
    return await refresh_source(target.source_id, triggered_by="scheduler")


def _summarise(targets: list[_Target], results: list[IngestionRun | None | BaseException]) -> int:
    """Log every outcome and count the successes. Never raises."""
    succeeded = 0
    for target, result in zip(targets, results, strict=True):
        if isinstance(result, Exception):
            # A genuine bug rather than a provider problem — `run_ingestion`
            # handles those itself and records them on the run row. The
            # traceback is the whole reason this is logged as an error.
            logger.error(
                "source ingestion raised",
                extra={"source": target.slug},
                exc_info=result,
            )
            continue
        if result is None or isinstance(result, BaseException):
            # Another worker holds this source's lock, or the row went away
            # between the SELECT and the run. `refresh_source` already logged
            # which; there is nothing left to add here.
            continue
        if result.status is RunStatus.failed and not target.configured:
            # Configuration, not health. Both arrive as the same status and they
            # call for opposite responses: "the API is down" wants a different
            # key, "nobody ever set the key" wants a config change. The run row
            # says `failed` either way — correct for the ledger — so this log
            # line is the only place the distinction survives.
            logger.warning(
                "source refresh aborted: not configured",
                extra={"source": target.slug, "reason": result.error},
            )
            continue
        succeeded += result.status is RunStatus.success
        logger.info("source ingested", extra={**_counters(result), "source": target.slug})
    return succeeded


def _counters(run: IngestionRun) -> dict[str, Any]:
    """The numbers an operator reads off a run, under non-reserved names.

    `created` and `updated` are reserved `LogRecord` attributes, and attaching
    one raises rather than silently dropping the field — which would turn a
    naming slip into an outage.
    """
    return {
        "status": run.status.value,
        "offers_created": run.created,
        "offers_updated": run.updated,
        "offers_failed": run.failed,
    }


async def refresh_source(
    source_id: Any,
    *,
    triggered_by: str | None = None,
    mode: SyncMode | None = None,
) -> IngestionRun | None:
    """Ingest one source, in its own session, under its own lock.

    Returns the run row, or `None` when there was nothing to do — another worker
    holds this source's lock, or the row has since been deleted. Raises only for
    genuine bugs; `run_ingestion` handles every source-level failure itself and
    reports it on the run row.

    Takes a `Source.id` rather than a `Source` for the reason `_Target` gives:
    a row loaded by a caller that then closes or rolls back its session cannot be
    read from here without tripping over attribute expiry, and a stale attached
    instance would have its health updates written to by nobody.
    """
    lock_id = source_lock_id(source_id)
    async with AsyncSessionLocal() as db:
        if not await try_lock(db, lock_id):
            logger.info(
                "source refresh skipped, another worker holds the lock",
                extra={"source_id": str(source_id)},
            )
            return None
        try:
            source = await db.get(Source, source_id)
            if source is None:
                logger.warning(
                    "source refresh skipped, row disappeared",
                    extra={"source_id": str(source_id)},
                )
                return None
            kwargs: dict[str, Any] = {"triggered_by": triggered_by}
            if mode is not None:
                kwargs["mode"] = mode
            return await run_ingestion(db, source, **kwargs)
        finally:
            await unlock(db, lock_id)


__all__ = [
    "MAX_CONCURRENT_SOURCES",
    "expire_stale_coupons",
    "is_source_due",
    "refresh_coupon_prices",
    "refresh_source",
]
