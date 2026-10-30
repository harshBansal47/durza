"""Ingestion controls for the web admin dashboard.

Read endpoints are open to admins and editors; starting a run is admin-only
because it makes outbound calls to third-party APIs.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.deps import require_role
from app.ingestion.credentials import credentials_satisfied
from app.models.ingestion_run import IngestionRun
from app.models.source import Source
from app.models.user import Role, User
from app.schemas.admin_ingestion import (
    IngestionOverview,
    IngestionRunOut,
    RunRequest,
    RunResponse,
    SchedulerState,
    SkippedSource,
    SourceStatusOut,
)
from app.services.manual_ingestion import start_ingestion_now

router = APIRouter(prefix="/admin/ingestion", tags=["admin-ingestion"])


def _scheduler_state() -> SchedulerState:
    settings = get_settings()
    names = settings.scheduler_job_names
    automated = settings.scheduler_enabled and (names is None or "refresh_prices" in names)
    return SchedulerState(
        enabled=settings.scheduler_enabled,
        ingestion_automated=automated,
        interval_hours=settings.ingestion_interval_hours,
    )


def _run_out(run: IngestionRun) -> IngestionRunOut:
    return IngestionRunOut(
        id=str(run.id),
        source_slug=run.source.slug,
        source_name=run.source.name,
        status=run.status.value,
        mode=run.mode.value,
        triggered_by=run.triggered_by,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_ms=run.duration_ms,
        fetched=run.fetched,
        created=run.created,
        updated=run.updated,
        duplicated=run.duplicated,
        failed=run.failed,
        malformed=run.malformed,
        error=run.error,
    )


@router.get("/overview", response_model=IngestionOverview)
async def get_ingestion_overview(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(Role.admin, Role.editor)),
) -> IngestionOverview:
    sources = list((await db.execute(select(Source).order_by(Source.slug))).scalars().all())
    rows: list[SourceStatusOut] = []
    for source in sources:
        latest = await db.scalar(
            select(IngestionRun)
            .where(IngestionRun.source_id == source.id)
            .order_by(IngestionRun.started_at.desc())
            .limit(1)
        )
        rows.append(
            SourceStatusOut(
                slug=source.slug,
                name=source.name,
                kind=source.kind.value,
                is_enabled=source.is_enabled,
                configured=credentials_satisfied(source.kind.value, source.credential_ref),
                api_status=source.api_status.value,
                consecutive_failures=source.consecutive_failures,
                sync_interval_minutes=source.sync_interval_minutes,
                last_sync_at=source.last_sync_at,
                last_success_at=source.last_success_at,
                last_failure_at=source.last_failure_at,
                last_error=source.last_error,
                latest_run_status=latest.status.value if latest else None,
                latest_run_started_at=latest.started_at if latest else None,
            )
        )
    return IngestionOverview(
        user_role=current_user.role.value,
        scheduler=_scheduler_state(),
        sources=rows,
    )


@router.get("/runs", response_model=list[IngestionRunOut])
async def list_ingestion_runs(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(Role.admin, Role.editor)),
) -> list[IngestionRunOut]:
    result = await db.execute(
        select(IngestionRun).order_by(IngestionRun.started_at.desc()).limit(limit)
    )
    return [_run_out(run) for run in result.scalars().all()]


@router.post("/run", response_model=RunResponse, status_code=202)
async def run_ingestion_now(
    body: RunRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(Role.admin)),
) -> RunResponse:
    """Start ingestion in the background. Poll /runs for the outcome."""
    stmt = select(Source).order_by(Source.slug)
    if body.source_slugs is not None:
        stmt = stmt.where(Source.slug.in_(body.source_slugs))
    sources = list((await db.execute(stmt)).scalars().all())

    unknown = sorted(set(body.source_slugs or []) - {source.slug for source in sources})
    if unknown:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {', '.join(unknown)}")

    runnable = [source.slug for source in sources if source.is_enabled]
    skipped = [
        SkippedSource(slug=source.slug, reason="disabled")
        for source in sources
        if not source.is_enabled
    ]
    if not runnable:
        raise HTTPException(status_code=409, detail="No enabled provider to run")

    start_ingestion_now(runnable, triggered_by=f"api:{current_user.id}")
    return RunResponse(started=runnable, skipped=skipped)
