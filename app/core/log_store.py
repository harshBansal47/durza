"""Ships application log records into the `app_logs` table.

stdout still carries every line and stays the source of truth. This adds a second
destination that can be filtered and counted from the admin, and that the
`scrub_logs` job empties on a schedule so it can never grow without bound.

A log handler is the one component that must not make the thing it observes
worse, which sets the design:

* `emit` does no I/O. It builds a row and appends it to an in-memory deque. One
  background task drains the deque in batches, so a slow database costs latency
  in the log and never in a request.
* The buffer is bounded. If the database is down long enough the oldest rows are
  discarded, not the process's memory.
* A failed write drops that batch and carries on. The handler never raises into
  application code and never retries forever.
* A burst of identical lines is throttled. A bot hammering one endpoint, or a
  provider failing in a loop, would otherwise write thousands of identical rows a
  minute. After `_BURST_LIMIT` of the same line in a window the rest are counted,
  and one summary row says how many were dropped.
* Not everything is stored. A line per page view is what metrics are for, so the
  access log is stored only at WARNING and above (slow, refused or failing
  requests). The app's own loggers are stored from `LOG_STORE_MIN_LEVEL`;
  third-party loggers only from WARNING.
* Nothing personal goes in. Email addresses are replaced with a fingerprint in the
  message, the fields and the traceback, whoever logged them, and fields named
  like secrets are redacted outright.
"""

import asyncio
import json
import logging
import time
from collections import deque
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import insert

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.core.observability import extra_fields, is_secret_key, request_id_var, scrub_emails
from app.models.app_log import AppLog

# This module's own logger is never stored: a failing write that logged itself
# into the buffer would feed on its own output.
logger = logging.getLogger("durza.logstore")

_ACCESS_LOGGER = "durza.access"
_MAX_BUFFER = 10_000
_MAX_MESSAGE = 2_000
_MAX_EXCEPTION = 6_000
_MAX_FIELD_VALUE = 500
_MAX_FIELDS_JSON = 4_000

# At most this many rows per distinct line per window; the rest are counted.
_BURST_LIMIT = 30
_BURST_WINDOW_SECONDS = 60.0
# Distinct lines remembered at once. Past this the table is cleared rather than
# grown, which only costs the pending summaries.
_MAX_TRACKED_LINES = 2_000


def _is_own(name: str) -> bool:
    return name == "durza" or name.startswith(("durza.", "app."))


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_emails(value)
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    return value


def _jsonable(value: Any) -> Any:
    try:
        cleaned = json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        cleaned = str(value)
    cleaned = _scrub(cleaned)
    if isinstance(cleaned, str):
        return cleaned[:_MAX_FIELD_VALUE]
    return cleaned


def _safe_fields(record: logging.LogRecord) -> dict[str, Any] | None:
    fields = {
        key: "[redacted]" if is_secret_key(key) else _jsonable(value)
        for key, value in extra_fields(record).items()
    }
    if not fields:
        return None
    if len(json.dumps(fields, default=str)) > _MAX_FIELDS_JSON:
        return {"truncated": True, "keys": sorted(fields)[:20]}
    return fields


def _row(record: logging.LogRecord) -> dict[str, Any]:
    request_id = request_id_var.get()
    exception = None
    if record.exc_info:
        trace = logging.Formatter().formatException(record.exc_info)
        exception = scrub_emails(trace)[:_MAX_EXCEPTION]
    return {
        "created_at": datetime.fromtimestamp(record.created, UTC),
        "level": record.levelname[:10],
        "logger": record.name[:100],
        "message": scrub_emails(record.getMessage())[:_MAX_MESSAGE],
        # Inbound X-Request-Id is caller-controlled, so it is clipped to the column.
        "request_id": request_id[:64] if request_id else None,
        "fields": _safe_fields(record),
        "exception": exception,
    }


def _summary_row(name: str, message: str, suppressed: int) -> dict[str, Any]:
    return {
        "created_at": datetime.now(UTC),
        "level": "WARNING",
        "logger": logger.name,
        "message": f"suppressed {suppressed} repeats of: {message}"[:_MAX_MESSAGE],
        "request_id": None,
        "fields": {"source_logger": name, "suppressed": suppressed},
        "exception": None,
    }


class DbLogHandler(logging.Handler):
    def __init__(self, min_level: int, *, max_buffer: int = _MAX_BUFFER) -> None:
        super().__init__()
        self._min_level = min_level
        self.buffer: deque[dict[str, Any]] = deque(maxlen=max_buffer)
        self.dropped = 0
        # line -> [window start, rows seen, rows suppressed]
        self._bursts: dict[tuple[str, int, str], list[float]] = {}

    def _wanted(self, record: logging.LogRecord) -> bool:
        name = record.name
        if name == logger.name or name.startswith("uvicorn.access"):
            return False
        if name == _ACCESS_LOGGER:
            return record.levelno >= logging.WARNING
        if _is_own(name):
            return record.levelno >= self._min_level
        return record.levelno >= logging.WARNING

    def _throttled(self, record: logging.LogRecord, row: dict[str, Any]) -> bool:
        """True if this line has already been written enough times this window.

        Called with the handler lock held (`Handler.handle` takes it), so the
        bookkeeping needs no lock of its own.
        """
        key = (record.name, record.levelno, row["message"][:200])
        now = time.monotonic()
        entry = self._bursts.get(key)
        if entry is None or now - entry[0] >= _BURST_WINDOW_SECONDS:
            if entry is not None and entry[2]:
                self._append(_summary_row(key[0], key[2], int(entry[2])))
            if len(self._bursts) >= _MAX_TRACKED_LINES:
                self._bursts.clear()
            self._bursts[key] = [now, 1.0, 0.0]
            return False
        entry[1] += 1
        if entry[1] <= _BURST_LIMIT:
            return False
        entry[2] += 1
        return True

    def collect_summaries(self) -> None:
        """Report what finished windows swallowed, even if the line never came back.

        Without this a flood that simply stops would leave its suppressed count
        unreported, which is the one case where the number matters most.
        """
        now = time.monotonic()
        self.acquire()
        try:
            for key, entry in list(self._bursts.items()):
                if now - entry[0] >= _BURST_WINDOW_SECONDS:
                    if entry[2]:
                        self._append(_summary_row(key[0], key[2], int(entry[2])))
                    del self._bursts[key]
        finally:
            self.release()

    def _append(self, row: dict[str, Any]) -> None:
        if len(self.buffer) == self.buffer.maxlen:
            self.dropped += 1
        self.buffer.append(row)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not self._wanted(record):
                return
            row = _row(record)
            if self._throttled(record, row):
                return
            self._append(row)
        except Exception:  # noqa: BLE001 - a log handler must never raise into the app
            self.handleError(record)


class LogStore:
    def __init__(
        self, handler: DbLogHandler, *, flush_seconds: float = 5.0, batch_size: int = 500
    ) -> None:
        self.handler = handler
        self._flush_seconds = flush_seconds
        self._batch_size = batch_size
        self._last_warned = 0.0

    async def flush(self) -> int:
        """Write everything buffered. Returns the number of rows written."""
        self.handler.collect_summaries()
        written = 0
        while self.handler.buffer:
            batch: list[dict[str, Any]] = []
            while self.handler.buffer and len(batch) < self._batch_size:
                batch.append(self.handler.buffer.popleft())
            try:
                async with AsyncSessionLocal() as db:
                    await db.execute(insert(AppLog), batch)
                    await db.commit()
            except Exception as exc:  # noqa: BLE001 - drop this batch rather than wedge the writer
                self._note_failure(len(batch), exc)
                break
            written += len(batch)
        return written

    def _note_failure(self, dropped: int, error: Exception) -> None:
        # At most one line a minute, or an outage would fill the log with this.
        now = time.monotonic()
        if now - self._last_warned >= 60:
            self._last_warned = now
            logger.warning("could not write log rows; dropped %d", dropped, exc_info=error)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._flush_seconds)
            await self.flush()


class _Running:
    store: LogStore | None = None
    task: asyncio.Task[None] | None = None


_running = _Running()


def start_log_store() -> bool:
    """Install the handler and start the writer. A no-op unless LOG_STORE_ENABLED."""
    settings = get_settings()
    if not settings.log_store_enabled or _running.store is not None:
        return False
    level = logging.getLevelNamesMapping().get(settings.log_store_min_level.upper(), logging.INFO)
    handler = DbLogHandler(level)
    logging.getLogger().addHandler(handler)
    _running.store = LogStore(handler)
    _running.task = asyncio.create_task(_running.store.run(), name="log-store")
    logger.info(
        "application log store started",
        extra={
            "min_level": settings.log_store_min_level,
            "retention_days": settings.log_retention_days,
        },
    )
    return True


async def stop_log_store() -> None:
    """Stop the writer and flush what is left, before the engine is disposed."""
    store = _running.store
    if store is None:
        return
    logging.getLogger().removeHandler(store.handler)
    if _running.task is not None:
        _running.task.cancel()
        with suppress(asyncio.CancelledError):
            await _running.task
    await store.flush()
    _running.store = None
    _running.task = None
