"""Coverage for the bounded, privacy-safe database log handler."""

import asyncio
import logging
import sys

import pytest

from app.core import log_store


def _record(name="app.example", level=logging.INFO, message="hello", **extra):
    record = logging.LogRecord(name, level, __file__, 1, message, (), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_handler_filters_loggers_and_levels():
    handler = log_store.DbLogHandler(logging.INFO)
    for record in (
        _record("durza.logstore", logging.ERROR),
        _record("uvicorn.access", logging.ERROR),
        _record("durza.access", logging.INFO),
        _record("app.example", logging.DEBUG),
        _record("vendor.lib", logging.INFO),
    ):
        handler.handle(record)
    assert list(handler.buffer) == []

    handler.handle(_record("durza.access", logging.WARNING))
    handler.handle(_record("vendor.lib", logging.WARNING))
    assert [row["logger"] for row in handler.buffer] == ["durza.access", "vendor.lib"]


def test_handler_scrubs_message_fields_and_exception_and_clips_request_id():
    handler = log_store.DbLogHandler(logging.INFO)
    email = "customer@example.com"
    log_store.request_id_var.set("r" * 70)
    try:
        try:
            raise ValueError(f"failed for {email}")
        except ValueError:
            record = _record(
                message=f"message for {email}",
                password="super-secret",
                context={"email": email},
            )
            record.exc_info = sys.exc_info()
            handler.handle(record)
    finally:
        log_store.request_id_var.set("")
    row = handler.buffer[0]
    assert email not in row["message"]
    assert row["request_id"] == "r" * 64
    assert row["fields"]["password"] == "[redacted]"
    assert email not in str(row["fields"])
    assert email not in row["exception"]


def test_handler_bounds_fields_and_buffer():
    handler = log_store.DbLogHandler(logging.INFO, max_buffer=1)
    handler.handle(_record(message="first"))
    fields = {f"key{i}": "x" * 500 for i in range(10)}
    handler.handle(_record(message="second", **fields))
    assert len(handler.buffer) == 1
    assert handler.dropped == 1
    assert handler.buffer[0]["fields"] == {
        "truncated": True,
        "keys": [f"key{i}" for i in range(10)],
    }


def test_throttle_summarizes_suppressed_repeats_when_window_expires(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(log_store.time, "monotonic", lambda: now[0])
    handler = log_store.DbLogHandler(logging.INFO)
    for _ in range(log_store._BURST_LIMIT + 2):
        handler.handle(_record(message="repeated"))
    assert len(handler.buffer) == log_store._BURST_LIMIT
    now[0] += log_store._BURST_WINDOW_SECONDS
    handler.handle(_record(message="repeated"))
    assert len(handler.buffer) == log_store._BURST_LIMIT + 2
    assert "suppressed 2 repeats" in handler.buffer[-2]["message"]


def test_collect_summaries_removes_expired_entries(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(log_store.time, "monotonic", lambda: now[0])
    handler = log_store.DbLogHandler(logging.INFO)
    handler._bursts[("app.example", logging.INFO, "line")] = [0.0, 31.0, 4.0]
    handler.collect_summaries()
    assert handler.buffer[0]["message"] == "suppressed 4 repeats of: line"
    assert handler._bursts == {}


@pytest.mark.asyncio
async def test_flush_batches_rows_and_commits(monkeypatch):
    batches = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def execute(self, statement, batch):
            batches.append(batch)

        async def commit(self):
            pass

    monkeypatch.setattr(log_store, "AsyncSessionLocal", Session)
    handler = log_store.DbLogHandler(logging.INFO)
    for i in range(3):
        handler.buffer.append({"message": str(i)})
    store = log_store.LogStore(handler, batch_size=2)
    assert await store.flush() == 3
    assert [len(batch) for batch in batches] == [2, 1]
    assert not handler.buffer


@pytest.mark.asyncio
async def test_flush_drops_failed_batch_and_throttles_failure_log(monkeypatch):
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def execute(self, *_):
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(log_store, "AsyncSessionLocal", Session)
    handler = log_store.DbLogHandler(logging.INFO)
    handler.buffer.append({"message": "lost"})
    store = log_store.LogStore(handler)
    store._last_warned = float("inf")
    assert await store.flush() == 0
    assert not handler.buffer


@pytest.mark.asyncio
async def test_start_and_stop_log_store(monkeypatch):
    class Settings:
        log_store_enabled = True
        log_store_min_level = "WARNING"
        log_retention_days = 14

    async def wait_forever():
        await asyncio.Event().wait()

    monkeypatch.setattr(log_store, "get_settings", lambda: Settings())
    monkeypatch.setattr(log_store.LogStore, "run", lambda self: wait_forever())
    before = list(logging.getLogger().handlers)
    try:
        assert log_store.start_log_store()
        assert not log_store.start_log_store()
        assert log_store._running.store.handler in logging.getLogger().handlers
        await log_store.stop_log_store()
        assert log_store._running.store is None
        await log_store.stop_log_store()
    finally:
        for handler in list(logging.getLogger().handlers):
            if handler not in before:
                logging.getLogger().removeHandler(handler)
        if log_store._running.store is not None:
            await log_store.stop_log_store()
