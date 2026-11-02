from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services import log_service


class _Rows:
    def __init__(self, rows=(), scalars=()):
        self.rows = rows
        self._scalars = scalars

    def all(self):
        return self._scalars if self._scalars else self.rows

    def scalars(self):
        return self

    def __iter__(self):
        return iter(self._scalars)


class _StatsDb:
    def __init__(self):
        self.results = [
            _Rows([("ERROR", 2), ("INFO", 3)]),
            _Rows([("app.api", 4, 2)]),
            _Rows([(datetime(2026, 1, 1, tzinfo=UTC), 5, 2)]),
            _Rows(scalars=["error-row"]),
        ]
        self.statements = []
        self.dialect = "sqlite"

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.pop(0)

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect))


@pytest.mark.asyncio
async def test_stats_and_search_build_expected_aggregates_and_optional_filters():
    db = _StatsDb()
    stats = await log_service.log_stats(db, hours=12)
    assert stats["total"] == 5
    assert stats["by_level"] == {"ERROR": 2, "INFO": 3}
    assert stats["loggers"] == [{"logger": "app.api", "total": 4, "problems": 2}]
    assert stats["hourly"][0]["hour"] == "2026-01-01T00:00:00+00:00"
    assert stats["recent_problems"] == ["error-row"]
    assert "strftime" in str(db.statements[2])

    db.results.append(_Rows(scalars=["match"]))
    await log_service.search_logs(
        db,
        hours=24,
        min_level="WARNING",
        logger_prefix="app.%",
        text="50% off",
        limit=10,
    )
    sql = db.statements[-1]
    assert str(sql).count("LIKE") == 2
    assert "app./%" in repr(sql.compile().params)
    assert "50/% off" in repr(sql.compile().params)


def test_hour_bucket_uses_postgres_expression_when_needed():
    db = _StatsDb()
    db.dialect = "postgresql"
    assert "date_trunc" in str(log_service._hour_bucket(db))


@pytest.mark.asyncio
async def test_purge_uses_at_least_one_day_and_stops_after_short_batch():
    class Result:
        pass

    class Db:
        def __init__(self):
            self.calls = []
            self.commits = 0

        async def execute(self, statement):
            self.calls.append(statement)
            result = Result()
            result.rowcount = log_service._PURGE_BATCH if len(self.calls) == 1 else 0
            return result

        async def commit(self):
            self.commits += 1

    db = Db()
    assert await log_service.purge_app_logs(db, older_than_days=-5) == log_service._PURGE_BATCH
    assert len(db.calls) == db.commits == 2
