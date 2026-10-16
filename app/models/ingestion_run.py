import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.source import Source


class RunStatus(str, enum.Enum):
    # `partial` is the important one. A provider whose page 40 of 60 failed has
    # told us everything it successfully told us; reporting that as `failed`
    # would throw away thousands of good records, and reporting it as `success`
    # would hide a feed that is quietly truncating. Partial is the honest answer
    # and it is what makes "CJ stopped returning page 41" visible on a dashboard.
    running = "running"
    success = "success"
    partial = "partial"
    failed = "failed"
    cancelled = "cancelled"


class SyncMode(str, enum.Enum):
    """How much of a provider's catalogue a run covers."""

    incremental = "incremental"  # default: changed / new / recently expiring only
    full = "full"  # everything the provider will give us


class IngestionRun(Base, UUIDPKMixin, TimestampMixin):
    """One execution of the ingestion pipeline against a Source — the audit trail.

    Every counter here exists to answer a question an operator actually asks:
    did it work, how much did we get, how much of it was new, how much was a
    duplicate of something we already had, and how much did we throw away. A
    run that only recorded success/failure cannot answer any of those, and a
    pipeline that silently discards malformed records looks identical to one
    where the provider returned nothing new.
    """

    __tablename__ = "ingestion_runs"

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, name="run_status", native_enum=True), nullable=False
    )
    mode: Mapped[SyncMode] = mapped_column(
        Enum(SyncMode, name="sync_mode", native_enum=True),
        default=SyncMode.incremental,
        server_default=SyncMode.incremental.value,
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ---- Record counts -----------------------------------------------------
    # fetched / created / updated / duplicated / failed / expired /
    # malformed. Every one of these answers a question an operator actually
    # asks, and the absence of any one of them makes a run indistinguishable
    # from a run that did nothing.
    fetched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Folded into a coupon another source had already provided. Not an error —
    # it is the dedupe layer doing its job, and the ratio is a direct measure of
    # how much redundancy the multi-source setup is actually absorbing.
    duplicated: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # Fetched and parsed fine, then rejected by validation: bad URL, impossible
    # discount, unknown category. The data was well-formed and unusable.
    # No server_default, matching the `fetched`/`created`/`updated` counters
    # added in the same earlier revision: these are written by the pipeline on
    # every path, including the failure path, so a DB-level default would only
    # mask a caller that forgets one.
    failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Could not be parsed at all. Worth separating from `failed` because a spike
    # here means the provider changed their payload shape, not that the data was
    # bad — a completely different fix.
    malformed: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # Expired on arrival — ingested and immediately retired, kept for history.
    expired: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    # ---- Diagnostics -------------------------------------------------------
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pages_fetched: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    # Set when an operator cancels a long run; distinguishes "stopped on
    # purpose" from "died", which matters when deciding whether to auto-retry.
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa.false(), nullable=False
    )
    # Who asked for this run: "scheduler", "admin", "cli", or a user id.
    triggered_by: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # Read-only convenience for the admin list and the health endpoint. Added
    # with the new fields rather than before because the old view showed only
    # raw ids and the run history is read constantly during an incident.
    source: Mapped["Source"] = relationship(lazy="joined")

    def __str__(self) -> str:
        return f"IngestionRun({self.source_id}, {self.status.value})"
