import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class LogSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class LogEntryOut(LogSchema):
    id: uuid.UUID
    created_at: datetime
    level: str
    logger: str
    message: str
    request_id: str | None
    fields: dict[str, Any] | None
    exception: str | None


class LoggerStat(LogSchema):
    logger: str
    total: int
    problems: int


class HourStat(LogSchema):
    hour: str
    total: int
    problems: int


class LogStats(LogSchema):
    window_hours: int
    #: False means nothing is being stored, so the numbers below are empty or stale.
    store_enabled: bool
    retention_days: int
    total: int
    by_level: dict[str, int]
    loggers: list[LoggerStat]
    hourly: list[HourStat]
    recent_problems: list[LogEntryOut]
