"""A searchable, self-expiring record of what the application did.

Not the durable history. `job_runs`, `ingestion_runs` and the click and revenue
tables are the permanent record of facts, and nothing here replaces them. This
is the working log: kept in a table so it can be filtered and counted from the
admin, and emptied by the `scrub_logs` job once rows pass `LOG_RETENTION_DAYS`.
See `app/core/log_store.py` for what gets written.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import UUIDPKMixin


class AppLog(Base, UUIDPKMixin):
    __tablename__ = "app_logs"
    __table_args__ = (Index("ix_app_logs_level_created_at", "level", "created_at"),)

    # Set by the writer from the record's own timestamp, so a row batched a few
    # seconds late still sorts where the event happened. The server default only
    # covers a row inserted by hand.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True, nullable=False
    )
    level: Mapped[str] = mapped_column(String(10), nullable=False)
    logger: Mapped[str] = mapped_column(String(100), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    fields: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    exception: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __str__(self) -> str:
        return f"{self.level} {self.logger}: {self.message[:60]}"
