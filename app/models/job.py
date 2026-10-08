"""History of scheduled job executions.

Not logging. Logs are ephemeral and get rotated; this table is the durable
record that answers "did ingestion run at 04:00, and if not, when did it last
succeed?" — a question the log aggregator can answer badly and this can answer
exactly.

Every attempt gets a row, including failures. A job that only records its
successes is indistinguishable from a job that stopped running.
"""

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin


class JobStatus(str, enum.Enum):
    running = "running"
    success = "success"
    failed = "failed"
    # Another replica held the advisory lock and did the work instead. Recorded
    # so the history reads honestly rather than showing gaps on every node.
    skipped = "skipped"


class JobRun(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "job_runs"

    # "expire_coupons" | "refresh_prices" | "send_alerts". Free text rather than
    # an enum: adding a job should not require a migration, and a typo in a job
    # name is a fact worth recording rather than crashing on insert.
    job: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status", native_enum=True), nullable=False
    )
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The annotation is the Python type; `DateTime` below is the column type.
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def __str__(self) -> str:
        return f"{self.job}:{self.status.value}"
