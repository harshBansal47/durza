"""Queries over the stored application log: retention, search and stats."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, delete, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_log import AppLog

PROBLEM_LEVELS = ("WARNING", "ERROR", "CRITICAL")
LEVEL_ORDER = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Deleted in slices so one scrub never holds a long lock on a busy table.
_PURGE_BATCH = 5000


async def purge_app_logs(db: AsyncSession, *, older_than_days: int) -> int:
    """Delete rows older than the retention window. Returns how many went."""
    cutoff = datetime.now(UTC) - timedelta(days=max(1, older_than_days))
    total = 0
    while True:
        oldest = select(AppLog.id).where(AppLog.created_at < cutoff).limit(_PURGE_BATCH)
        result = await db.execute(delete(AppLog).where(AppLog.id.in_(oldest)))
        await db.commit()
        removed = int(result.rowcount or 0)  # type: ignore[attr-defined]
        total += removed
        if removed < _PURGE_BATCH:
            return total


def _hour_bucket(db: AsyncSession) -> Any:
    # The literal is inlined, not bound: Postgres will not accept a SELECT
    # expression and a GROUP BY expression that differ only in parameter number.
    if db.get_bind().dialect.name == "postgresql":
        return func.date_trunc(literal_column("'hour'"), AppLog.created_at)
    return func.strftime(literal_column("'%Y-%m-%dT%H:00:00'"), AppLog.created_at)


async def log_stats(db: AsyncSession, *, hours: int) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(hours=hours)
    in_window = AppLog.created_at >= since
    problems = func.sum(case((AppLog.level.in_(PROBLEM_LEVELS), 1), else_=0))

    level_rows = (
        await db.execute(select(AppLog.level, func.count()).where(in_window).group_by(AppLog.level))
    ).all()
    logger_rows = (
        await db.execute(
            select(AppLog.logger, func.count(), problems)
            .where(in_window)
            .group_by(AppLog.logger)
            .order_by(func.count().desc())
            .limit(15)
        )
    ).all()
    bucket = _hour_bucket(db)
    hour_rows = (
        await db.execute(
            select(bucket, func.count(), problems)
            .where(in_window)
            .group_by(bucket)
            .order_by(bucket)
        )
    ).all()
    recent = list(
        (
            await db.execute(
                select(AppLog)
                .where(in_window, AppLog.level.in_(PROBLEM_LEVELS))
                .order_by(AppLog.created_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )

    def _hour(value: Any) -> str:
        return value.isoformat() if isinstance(value, datetime) else str(value)

    return {
        "total": sum(count for _level, count in level_rows),
        "by_level": {level: count for level, count in level_rows},
        "loggers": [
            {"logger": name, "total": total, "problems": int(bad or 0)}
            for name, total, bad in logger_rows
        ],
        "hourly": [
            {"hour": _hour(hour), "total": total, "problems": int(bad or 0)}
            for hour, total, bad in hour_rows
        ],
        "recent_problems": recent,
    }


async def search_logs(
    db: AsyncSession,
    *,
    hours: int,
    min_level: str | None,
    logger_prefix: str | None,
    text: str | None,
    limit: int,
) -> list[AppLog]:
    since = datetime.now(UTC) - timedelta(hours=hours)
    stmt = select(AppLog).where(AppLog.created_at >= since)
    if min_level:
        stmt = stmt.where(AppLog.level.in_(LEVEL_ORDER[LEVEL_ORDER.index(min_level) :]))
    if logger_prefix:
        stmt = stmt.where(AppLog.logger.startswith(logger_prefix, autoescape=True))
    if text:
        stmt = stmt.where(AppLog.message.icontains(text, autoescape=True))
    result = await db.execute(stmt.order_by(AppLog.created_at.desc()).limit(limit))
    return list(result.scalars().all())
