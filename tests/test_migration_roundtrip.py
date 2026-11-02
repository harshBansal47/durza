from types import SimpleNamespace

import pytest

from scripts import migration_roundtrip


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class _Connection:
    def __init__(self, category_count):
        self.category_count = category_count

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def run_sync(self, callback):
        return callback(None)

    async def execute(self, statement, parameters=None):
        query = str(statement)
        if "FROM categories WHERE" in query:
            return _Result(1)
        if "FROM categories" in query:
            return _Result(self.category_count)
        return _Result(0)


@pytest.mark.asyncio
async def test_roundtrip_check_accepts_only_the_migration_seed_category(monkeypatch):
    class Engine:
        def connect(self):
            return _Connection(category_count=1)

    monkeypatch.setattr(migration_roundtrip, "engine", Engine())
    monkeypatch.setattr(
        migration_roundtrip,
        "Schema",
        lambda _conn: SimpleNamespace(tables=["categories", "coupons"]),
    )
    assert await migration_roundtrip.check() == 0


@pytest.mark.asyncio
async def test_roundtrip_check_rejects_leftover_rows_with_expected_seed(monkeypatch):
    class Engine:
        def connect(self):
            return _Connection(category_count=2)

    monkeypatch.setattr(migration_roundtrip, "engine", Engine())
    monkeypatch.setattr(
        migration_roundtrip,
        "Schema",
        lambda _conn: SimpleNamespace(tables=["categories"]),
    )
    assert await migration_roundtrip.check() == 1
