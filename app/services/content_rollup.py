"""Idempotent content analytics rollups."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analytics_event import AnalyticsEvent
from app.models.daily_stats import DailyContentStats


async def recompute_content_rollups(db: AsyncSession, *, days: int = 90) -> None:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    stmt = (
        select(
            func.date(AnalyticsEvent.created_at).label("day"),
            AnalyticsEvent.content_id,
            AnalyticsEvent.content_type,
            AnalyticsEvent.name,
            func.count().label("n"),
        )
        .where(AnalyticsEvent.created_at >= cutoff, AnalyticsEvent.content_id.is_not(None))
        .group_by(
            func.date(AnalyticsEvent.created_at),
            AnalyticsEvent.content_id,
            AnalyticsEvent.content_type,
            AnalyticsEvent.name,
        )
    )
    rows = (await db.execute(stmt)).all()
    agg: dict[tuple, dict[str, int]] = {}
    for r in rows:
        key = (r.day, r.content_id, r.content_type or "guide")
        agg.setdefault(
            key,
            {
                "views": 0,
                "scroll_25": 0,
                "scroll_50": 0,
                "scroll_75": 0,
                "scroll_100": 0,
                "engaged": 0,
                "engaged_count": 0,
                "outbound": 0,
            },
        )
        if r.name == "content_view":
            agg[key]["views"] += r.n
        elif r.name == "scroll":
            # bucket in props not available here; count as generic scroll
            agg[key]["scroll_25"] += r.n
        elif r.name == "engaged_time":
            agg[key]["engaged"] += r.n
            agg[key]["engaged_count"] += r.n
        elif r.name == "outbound_click":
            agg[key]["outbound"] += r.n

    for (day, ref, ctype), vals in agg.items():
        existing = (
            await db.execute(
                select(DailyContentStats).where(
                    DailyContentStats.date == day,
                    DailyContentStats.content_ref == ref,
                    DailyContentStats.content_type == ctype,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = DailyContentStats(date=day, content_ref=ref, content_type=ctype)
            db.add(existing)
        existing.views = vals["views"]
        existing.scroll_25 = vals["scroll_25"]
        existing.outbound_clicks = vals["outbound"]
        existing.engaged_time_sum_ms = vals["engaged"]
        existing.engaged_time_count = vals["engaged_count"]
    await db.commit()
