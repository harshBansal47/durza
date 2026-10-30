"""Start ingestion on demand, outside the schedule.

Shared by the SQLAdmin "Ingest now" action and the JSON admin API, so both take
the same path: the scheduler's `refresh_prices` job, forced, narrowed to the
chosen providers. That keeps the locks, the job ledger and the per-source
failure handling identical to a scheduled run.
"""

import asyncio
import logging

from app.services.scheduler import run_job_now

logger = logging.getLogger(__name__)

# Held so a background run is not garbage-collected mid-flight.
_running: set[asyncio.Task[None]] = set()


async def _run(slugs: list[str]) -> None:
    try:
        detail = await run_job_now("refresh_prices", force=True, source_slugs=slugs)
    except Exception:
        logger.exception("manual ingestion failed", extra={"sources": slugs})
    else:
        logger.info("manual ingestion finished", extra={"sources": slugs, "detail": detail})


def start_ingestion_now(slugs: list[str]) -> None:
    """Fire-and-forget: returns at once, the outcome lands in Ingestion Runs."""
    task = asyncio.create_task(_run(slugs))
    _running.add(task)
    task.add_done_callback(_running.discard)
