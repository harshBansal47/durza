from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class IngestionSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class SchedulerState(IngestionSchema):
    #: The scheduler process is on at all.
    enabled: bool
    #: Ingestion specifically runs on a timer. False means manual-only.
    ingestion_automated: bool
    interval_hours: int


class SourceStatusOut(IngestionSchema):
    slug: str
    name: str
    kind: str
    is_enabled: bool
    #: Credentials named by the source are present in this process's environment.
    configured: bool
    api_status: str
    consecutive_failures: int
    sync_interval_minutes: int
    last_sync_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_error: str | None
    latest_run_status: str | None
    latest_run_started_at: datetime | None


class IngestionOverview(IngestionSchema):
    user_role: str
    scheduler: SchedulerState
    sources: list[SourceStatusOut]


class IngestionRunOut(IngestionSchema):
    id: str
    source_slug: str
    source_name: str
    status: str
    mode: str
    triggered_by: str | None
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    fetched: int
    created: int
    updated: int
    duplicated: int
    failed: int
    malformed: int
    error: str | None


class RunRequest(IngestionSchema):
    #: Omit to run every enabled provider.
    source_slugs: list[str] | None = Field(default=None, min_length=1, max_length=50)


class SkippedSource(IngestionSchema):
    slug: str
    reason: str


class RunResponse(IngestionSchema):
    started: list[str]
    skipped: list[SkippedSource]
