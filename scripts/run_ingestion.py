"""Run the ingestion pipeline for one source (or all enabled sources).

Usage (inside the app container):
    python scripts/run_ingestion.py --source-slug affiliate-feed
    python scripts/run_ingestion.py --all
"""

import argparse
import asyncio

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.ingestion.pipeline import run_ingestion
from app.models.source import Source


async def run(source_slug: str | None, all_sources: bool) -> None:
    async with AsyncSessionLocal() as db:
        stmt = select(Source)
        if not all_sources:
            stmt = stmt.where(Source.slug == source_slug)
        result = await db.execute(stmt)
        sources = list(result.scalars().all())
        if not sources:
            print("No matching source found.")
            return
        for source in sources:
            run_row = await run_ingestion(db, source)
            print(
                f"[{source.slug}] status={run_row.status.value} fetched={run_row.fetched} "
                f"created={run_row.created} updated={run_row.updated} failed={run_row.failed} "
                f"error={run_row.error or '-'}"
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run coupon ingestion")
    parser.add_argument("--source-slug", default=None)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    if not args.all and not args.source_slug:
        parser.error("pass --source-slug X or --all")
    asyncio.run(run(args.source_slug, args.all))
