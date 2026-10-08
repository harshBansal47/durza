"""Expire stale coupons, then re-run ingestion so live data refreshes.

Intended to be run on a schedule (cron / systemd timer / k8s CronJob):
    0 */6 * * * docker compose exec -T app python scripts/refresh_coupons.py
"""

import argparse
import asyncio

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.ingestion.lifecycle import expire_stale_coupons
from app.ingestion.pipeline import run_ingestion
from app.models.source import Source


async def refresh(run_sources: bool) -> None:
    async with AsyncSessionLocal() as db:
        expired = await expire_stale_coupons(db)
        print(f"Expired {expired} stale coupon(s).")
        if not run_sources:
            return
        result = await db.execute(select(Source).where(Source.is_enabled.is_(True)))
        for source in result.scalars().all():
            run_row = await run_ingestion(db, source)
            print(
                f"[{source.slug}] {run_row.status.value}: +{run_row.created} created, "
                f"{run_row.updated} updated, {run_row.failed} failed"
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Expire + refresh coupon data")
    parser.add_argument("--skip-sources", action="store_true", help="only expire, don't re-ingest")
    args = parser.parse_args()
    asyncio.run(refresh(not args.skip_sources))
