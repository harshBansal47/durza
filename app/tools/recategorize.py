"""Re-file coupons that are parked in the fallback category.

    podman-compose run --rm --no-deps -T app python -m app.tools.recategorize [--dry-run]

For after the taxonomy or the rules change. Create a "Home & Kitchen" category, or add
a keyword, and the offers that were waiting in "Other" for it can move. Safe to re-run:
it only moves coupons that are still in the fallback and now resolve somewhere better,
and `--dry-run` shows what it would do without changing anything.

Mapping a wording in the admin already moves that wording's coupons; this is for
everything else at once.
"""

import argparse
import asyncio

from app.core.database import AsyncSessionLocal
from app.ingestion.categories import CategoryResolver, recategorize_fallback


async def run(*, dry_run: bool) -> int:
    async with AsyncSessionLocal() as db:
        moved = await recategorize_fallback(db, CategoryResolver(), dry_run=dry_run)
    if not moved:
        print("Nothing to move: no coupon in the fallback category resolves anywhere better.")
        return 0
    verb = "would move" if dry_run else "moved"
    for slug, count in sorted(moved.items(), key=lambda item: -item[1]):
        print(f"{verb} {count:>5} coupon(s) -> {slug}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report, change nothing")
    return asyncio.run(run(dry_run=parser.parse_args().dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
