"""Stats and search over the stored application log. Admin only.

Log lines can carry operational detail (routes, error text, pseudonymised
identifiers), so unlike the ingestion status endpoints these are not open to
editors.
"""

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.deps import require_role
from app.models.user import Role, User
from app.schemas.admin_logs import HourStat, LogEntryOut, LoggerStat, LogStats
from app.services import log_service

router = APIRouter(prefix="/admin/logs", tags=["admin-logs"])

Level = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


@router.get("/stats", response_model=LogStats)
async def get_log_stats(
    hours: int = Query(24, ge=1, le=24 * 30),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(Role.admin)),
) -> LogStats:
    settings = get_settings()
    data = await log_service.log_stats(db, hours=hours)
    return LogStats(
        window_hours=hours,
        store_enabled=settings.log_store_enabled,
        retention_days=settings.log_retention_days,
        total=data["total"],
        by_level=data["by_level"],
        loggers=[LoggerStat(**row) for row in data["loggers"]],
        hourly=[HourStat(**row) for row in data["hourly"]],
        recent_problems=[LogEntryOut.model_validate(row) for row in data["recent_problems"]],
    )


@router.get("", response_model=list[LogEntryOut])
async def search_logs(
    hours: int = Query(24, ge=1, le=24 * 30),
    min_level: Level | None = None,
    logger: str | None = Query(None, max_length=100, description="Logger name prefix"),
    q: str | None = Query(None, max_length=200, description="Text in the message"),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(Role.admin)),
) -> list[LogEntryOut]:
    rows = await log_service.search_logs(
        db, hours=hours, min_level=min_level, logger_prefix=logger, text=q, limit=limit
    )
    return [LogEntryOut.model_validate(row) for row in rows]
