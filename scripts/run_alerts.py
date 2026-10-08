"""Scan all tracked products and fire due alerts. Cron alongside refresh_coupons.py:

5,35 * * * * docker compose exec -T app python scripts/run_alerts.py
"""

import asyncio

from app.core.database import AsyncSessionLocal
from app.services.alert_service import run_alert_scan


async def main() -> None:
    async with AsyncSessionLocal() as db:
        sent = await run_alert_scan(db)
        print(f"Alerts sent: {sent}")


if __name__ == "__main__":
    asyncio.run(main())
