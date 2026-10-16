"""Per-source scheduling: who runs, when, and what happens when one of them breaks.

The properties worth protecting, in the order they matter:

* **A source is synced on its own interval, not the sweep's.** One sweep ticking
  every six hours has to be able to refresh an hourly provider while leaving a
  degraded one alone, or the whole catalogue inherits the slowest provider's
  schedule.
* **One source's failure is one source's failure.** A provider that 500s on every
  request must not stop the other three from updating, and must not leave the
  transaction such that the ones after it fail too.
* **Due-ness reads the same interval function everything else does.** A second
  implementation of the health multiplier is a second thing to get wrong.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core import locks
from app.ingestion import registry
from app.ingestion.base import PartialFetchError, RawOffer
from app.ingestion.source_health import effective_interval_minutes
from app.models.category import Category
from app.models.coupon import Coupon
from app.models.ingestion_run import IngestionRun, RunStatus
from app.models.source import Source, SourceKind, SourceStatus
from app.services import lifecycle_service, scheduler
from app.services.lifecycle_service import is_source_due, refresh_coupon_prices


@pytest.fixture(autouse=True)
def _use_test_database(monkeypatch, async_session_maker):
    """Point the per-source sessions at the throwaway test database.

    `refresh_source` opens its own session from the module-level
    `AsyncSessionLocal`, so without this every source would be ingested into the
    real Postgres while the assertions ran against an empty in-memory database.
    """
    monkeypatch.setattr(lifecycle_service, "AsyncSessionLocal", async_session_maker)


@pytest.fixture(autouse=True)
def _use_test_database_for_scheduler(monkeypatch, async_session_maker):
    monkeypatch.setattr(scheduler, "AsyncSessionLocal", async_session_maker)
    scheduler._failure_streak.clear()
    scheduler._backoff_until.clear()


@pytest.fixture
async def category(async_session_maker):
    async with async_session_maker() as db:
        cat = Category(name="Fashion", slug="fashion")
        db.add(cat)
        await db.commit()
        return cat.id


def _offer(store_slug: str, *, code: str | None = None, title: str | None = None) -> dict:
    # The code is part of the offer's identity, so two fixtures sharing one would
    # be folded into a single coupon by the dedupe layer and the test would be
    # asserting on the wrong thing. Same for the destination: `shop01.example.com`
    # and `shop02.example.com` are one merchant, which is the whole point of the
    # domain-normalising funnel and is tested in `test_ingestion.py`.
    return {
        "title": title or f"Deal at {store_slug}",
        "store_slug": store_slug,
        "category_slug": "fashion",
        "code": code or f"SAVE-{store_slug.upper()}",
        "discount_text": "20% off",
        "destination_url": f"https://{store_slug}.example.com/deal",
    }


async def _add_source(
    async_session_maker,
    *,
    slug: str,
    offers: list[dict] | None = None,
    kind: SourceKind = SourceKind.static,
    config: dict | None = None,
    **kwargs,
) -> str:
    payload = {
        "name": slug.replace("-", " ").title(),
        "slug": slug,
        "kind": kind,
        "config": config if config is not None else {"offers": offers or []},
        **kwargs,
    }
    async with async_session_maker() as db:
        source = Source(**payload)
        db.add(source)
        await db.commit()
        return source.id


def _unsaved_source(**overrides) -> Source:
    """A `Source` with every defaulted attribute populated.

    The column defaults are applied by the INSERT, so an unsaved row would have
    `sync_interval_minutes is None` and the interval maths would raise rather
    than quietly mean something else.
    """
    payload = {
        "name": "Test",
        "slug": "test",
        "kind": SourceKind.static,
        "config": {},
        "is_enabled": True,
        "sync_interval_minutes": 360,
        "api_status": SourceStatus.healthy,
    }
    payload.update(overrides)
    return Source(**payload)


async def _runs(async_session_maker, source_id: str) -> list[IngestionRun]:
    async with async_session_maker() as db:
        stmt = select(IngestionRun).where(IngestionRun.source_id == source_id)
        return list((await db.execute(stmt)).scalars().all())


async def _coupon_titles(async_session_maker) -> set[str]:
    async with async_session_maker() as db:
        return set((await db.execute(select(Coupon.title))).scalars().all())


# ---------------------------------------------------------------------------
# Lock ids
# ---------------------------------------------------------------------------


def test_a_source_lock_id_is_stable_across_processes() -> None:
    # Stability is the entire point. Python's `hash()` is randomised per process,
    # so two replicas would derive different ids for the same source and the lock
    # would protect nothing while still appearing to work.
    source_id = uuid.uuid4()
    assert locks.source_lock_id(source_id) == locks.source_lock_id(source_id)


def test_a_source_lock_id_differs_per_source() -> None:
    ids = {locks.source_lock_id(uuid.uuid4()) for _ in range(500)}
    assert len(ids) == 500


def test_a_source_lock_id_is_a_signed_64_bit_negative_integer() -> None:
    # Forced negative so the namespace is provably disjoint from the scheduler's
    # positive job constants. Postgres takes a signed 64-bit integer, so an id
    # outside that range is a runtime error, not a near-miss.
    value = locks.source_lock_id(uuid.uuid4())
    assert -(2**63) <= value <= -1


def test_no_source_lock_id_can_collide_with_a_job_lock() -> None:
    source_ids = [locks.source_lock_id(uuid.uuid4()) for _ in range(2000)]
    assert set(source_ids).isdisjoint({lock for _fn, lock in scheduler.JOBS.values()})


def test_lock_ids_are_the_same_for_a_string_and_a_uuid() -> None:
    # `Source.id` is a UUID column but is also passed around as `object`, and
    # `str()` of it is stable — the two spellings must not mean different locks.
    source_id = uuid.uuid4()
    assert locks.source_lock_id(source_id) == locks.source_lock_id(str(source_id))


# ---------------------------------------------------------------------------
# Due-ness
# ---------------------------------------------------------------------------


def test_a_source_that_has_never_run_is_due() -> None:
    # Otherwise a newly added provider sits idle until somebody notices the empty
    # row, which is precisely the failure that is invisible.
    assert is_source_due(_unsaved_source()) is True


def test_a_source_synced_moments_ago_is_not_due() -> None:
    now = datetime.now(UTC)
    source = _unsaved_source(last_sync_at=now - timedelta(minutes=5))
    assert is_source_due(source, now=now) is False


def test_a_source_is_due_once_its_own_interval_has_elapsed() -> None:
    now = datetime.now(UTC)
    interval = 360
    just_short = _unsaved_source(
        sync_interval_minutes=interval,
        last_sync_at=now - timedelta(minutes=interval - 1),
    )
    elapsed = _unsaved_source(
        sync_interval_minutes=interval,
        last_sync_at=now - timedelta(minutes=interval + 1),
    )
    assert is_source_due(just_short, now=now) is False
    assert is_source_due(elapsed, now=now) is True


def test_a_degraded_source_waits_longer_than_a_healthy_one() -> None:
    # The whole reason `effective_interval_minutes` exists: four healthy
    # providers must not queue behind one that is timing out. If due-ness read the
    # raw `sync_interval_minutes` this test would fail, and so would the design.
    now = datetime.now(UTC)
    synced = now - timedelta(minutes=500)

    healthy = _unsaved_source(
        sync_interval_minutes=360, api_status=SourceStatus.healthy, last_sync_at=synced
    )
    degraded = _unsaved_source(
        sync_interval_minutes=360, api_status=SourceStatus.degraded, last_sync_at=synced
    )
    failing = _unsaved_source(
        sync_interval_minutes=360, api_status=SourceStatus.failing, last_sync_at=synced
    )

    assert effective_interval_minutes(healthy) == 360
    assert is_source_due(healthy, now=now) is True
    assert is_source_due(degraded, now=now) is False
    assert is_source_due(failing, now=now) is False

    much_later = now + timedelta(minutes=effective_interval_minutes(degraded))
    assert is_source_due(degraded, now=much_later) is True


def test_a_naive_last_sync_at_is_read_as_utc() -> None:
    # The test database is SQLite, which hands back naive datetimes from
    # `timestamptz` columns. Comparing that with an aware `now()` raises
    # `TypeError` deep inside a function that has nothing to do with timezones.
    now = datetime.now(UTC)
    source = _unsaved_source(last_sync_at=(now - timedelta(minutes=500)).replace(tzinfo=None))
    assert is_source_due(source, now=now) is True


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


async def test_only_sources_that_are_due_are_ingested(async_session_maker, category) -> None:
    fresh = await _add_source(
        async_session_maker,
        slug="fresh",
        offers=[_offer("freshshop")],
        last_sync_at=datetime.now(UTC),
    )
    stale = await _add_source(
        async_session_maker,
        slug="stale",
        offers=[_offer("staleshop")],
        last_sync_at=datetime.now(UTC) - timedelta(minutes=400),
    )

    async with async_session_maker() as db:
        assert await refresh_coupon_prices(db) == 1

    assert len(await _runs(async_session_maker, stale)) == 1
    assert await _runs(async_session_maker, fresh) == []
    assert await _coupon_titles(async_session_maker) == {"Deal at staleshop"}


async def test_force_ignores_the_interval(async_session_maker, category) -> None:
    # The operator's override. Without it there is no way to re-ingest a provider
    # that was skipped, short of editing `last_sync_at` by hand.
    await _add_source(
        async_session_maker,
        slug="fresh",
        offers=[_offer("freshshop")],
        last_sync_at=datetime.now(UTC),
    )

    async with async_session_maker() as db:
        assert await refresh_coupon_prices(db, force=True) == 1

    assert await _coupon_titles(async_session_maker) == {"Deal at freshshop"}


async def test_a_disabled_source_is_not_swept_at_all(async_session_maker, category) -> None:
    # `is_enabled` is a person's decision. Sweeping the row only for the pipeline
    # to refuse it fills the run ledger with rows whose only content is "the
    # scheduler is running".
    paused = await _add_source(
        async_session_maker, slug="paused", offers=[_offer("pausedshop")], is_enabled=False
    )

    async with async_session_maker() as db:
        assert await refresh_coupon_prices(db, force=True) == 0

    assert await _runs(async_session_maker, paused) == []
    assert await _coupon_titles(async_session_maker) == set()


async def test_one_failing_source_does_not_stop_the_others(async_session_maker, category) -> None:
    # Ordered by slug, so the broken one is genuinely in the middle: a shared
    # session would leave every source after it failing for the wrong reason.
    await _add_source(async_session_maker, slug="a-ok", offers=[_offer("ashop")])
    broken = await _add_source(
        async_session_maker, slug="b-broken", kind=SourceKind.feed, config={}
    )
    await _add_source(async_session_maker, slug="c-ok", offers=[_offer("cshop")])

    async with async_session_maker() as db:
        assert await refresh_coupon_prices(db) == 2

    assert await _coupon_titles(async_session_maker) == {"Deal at ashop", "Deal at cshop"}
    broken_runs = await _runs(async_session_maker, broken)
    assert [r.status for r in broken_runs] == [RunStatus.failed]
    # The reason survives onto the run row, so this is diagnosable without logs.
    assert "url" in (broken_runs[0].error or "")


async def test_a_source_that_raises_does_not_abort_the_sweep(
    async_session_maker, category, caplog, monkeypatch
) -> None:
    # `run_ingestion` handles provider failures itself; this covers a genuine bug
    # escaping it, which must still cost exactly one source.
    broken = await _add_source(async_session_maker, slug="a-boom", offers=[_offer("ashop")])
    ok = await _add_source(async_session_maker, slug="b-ok", offers=[_offer("bshop")])

    real = lifecycle_service.run_ingestion

    async def _sometimes_buggy(db, source, **kwargs):
        if source.id == broken:
            raise AttributeError("'NoneType' object has no attribute 'config'")
        return await real(db, source, **kwargs)

    monkeypatch.setattr(lifecycle_service, "run_ingestion", _sometimes_buggy)

    async with async_session_maker() as db:
        assert await refresh_coupon_prices(db) == 1

    assert await _coupon_titles(async_session_maker) == {"Deal at bshop"}
    assert [r.status for r in await _runs(async_session_maker, ok)] == [RunStatus.success]
    # No run row, because the exception escaped before the pipeline could write
    # one — which is exactly why the sweep has to log the traceback itself. A
    # silently swallowed bug is the one failure mode this whole design promises
    # is impossible.
    assert await _runs(async_session_maker, broken) == []
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("source ingestion raised" in r.getMessage() for r in errors)
    assert any(r.exc_info for r in errors)


async def test_a_missing_credential_is_reported_as_configuration_not_health(
    async_session_maker, caplog, monkeypatch
) -> None:
    # `cj` needs a credential and none is configured. That is an unfinished
    # deployment step, not a provider outage, and the two call for opposite
    # responses — so it must not be charged against the source's health score.
    monkeypatch.delenv("CJ_API_KEY", raising=False)
    source_id = await _add_source(
        async_session_maker, slug="cj", kind=SourceKind.cj, config={"website_id": "1"}
    )

    async with async_session_maker() as db:
        assert await refresh_coupon_prices(db) == 0

    messages = [r.getMessage() for r in caplog.records]
    assert "source refresh aborted: not configured" in messages
    # And it is not logged as a completed ingest, which would read as "the sweep
    # ran and CJ answered".
    assert "source ingested" not in messages

    async with async_session_maker() as db:
        source = await db.get(Source, source_id)
        assert source.consecutive_failures == 0
        assert source.api_status is SourceStatus.unconfigured


async def test_a_partial_run_is_not_counted_as_a_success(
    async_session_maker, category, monkeypatch
) -> None:
    # A provider that returned 1 page of 40 has not succeeded. Reporting it as
    # one would hide a feed that is quietly truncating — and would keep marking
    # its stale rows down while the other 39 pages go unretrieved.
    class _TruncatingSource:
        async def stream(self, config, *, mode=None, credentials=None):
            yield RawOffer(
                title="Kept deal",
                store_slug="truncatedshop",
                category_slug="fashion",
                code="KEPT",
                discount_text="10% off",
                destination_url="https://truncatedshop.example.com/deal",
            )
            raise PartialFetchError("failed on page 2 of 40", pages_completed=2)

    monkeypatch.setitem(registry._ADAPTERS, SourceKind.static, _TruncatingSource)
    source_id = await _add_source(async_session_maker, slug="truncating")

    async with async_session_maker() as db:
        assert await refresh_coupon_prices(db) == 0

    runs = await _runs(async_session_maker, source_id)
    assert [r.status for r in runs] == [RunStatus.partial]
    assert "page 2 of 40" in (runs[0].error or "")
    # What the provider did manage to send is kept — that is the difference
    # between `partial` and `failed`.
    assert await _coupon_titles(async_session_maker) == {"Kept deal"}


async def test_a_sweep_with_nothing_due_still_says_so(async_session_maker, caplog) -> None:
    # Silence here would be indistinguishable from a scheduler that stopped.
    await _add_source(
        async_session_maker,
        slug="fresh",
        offers=[_offer("freshshop")],
        last_sync_at=datetime.now(UTC),
    )

    async with async_session_maker() as db:
        assert await refresh_coupon_prices(db) == 0

    assert any("source refresh sweep starting" in r.getMessage() for r in caplog.records)


async def test_the_sweep_does_not_hold_its_read_transaction_open(
    async_session_maker, category
) -> None:
    # Each source runs in its own session. If the sweep kept its read
    # transaction open, those sessions would be stacked on a connection the
    # sweep is still using and the whole thing would serialise by accident.
    await _add_source(async_session_maker, slug="only", offers=[_offer("oneshop")])

    async with async_session_maker() as db:
        await refresh_coupon_prices(db)
        assert db.in_transaction() is False


# ---------------------------------------------------------------------------
# One source on its own
# ---------------------------------------------------------------------------


async def test_refresh_source_returns_none_when_another_worker_holds_the_lock(
    async_session_maker, monkeypatch
) -> None:
    source_id = await _add_source(async_session_maker, slug="held", offers=[_offer("heldshop")])

    async def _locked(db, lock_id):
        return False

    monkeypatch.setattr(lifecycle_service, "try_lock", _locked)
    assert await lifecycle_service.refresh_source(source_id) is None


async def test_refresh_source_returns_none_for_a_deleted_row(async_session_maker) -> None:
    assert await lifecycle_service.refresh_source(uuid.uuid4()) is None


async def test_refresh_source_releases_its_lock_even_when_ingestion_raises(
    async_session_maker, monkeypatch
) -> None:
    # A leaked advisory lock is held by a pooled connection until the process
    # exits, so every later attempt reports "another worker held the lock"
    # forever. The `finally` is the only thing preventing that.
    source_id = await _add_source(async_session_maker, slug="boom", offers=[_offer("boomshop")])
    released: list[int] = []

    async def _bug(db, source, **kwargs):
        raise RuntimeError("kaboom")

    async def _record_unlock(db, lock_id):
        released.append(lock_id)

    monkeypatch.setattr(lifecycle_service, "run_ingestion", _bug)
    monkeypatch.setattr(lifecycle_service, "unlock", _record_unlock)

    with pytest.raises(RuntimeError):
        await lifecycle_service.refresh_source(source_id)

    assert released == [locks.source_lock_id(source_id)]


async def test_refresh_source_stamps_the_run_with_who_asked(async_session_maker, category) -> None:
    source_id = await _add_source(async_session_maker, slug="traced", offers=[_offer("traceshop")])
    await lifecycle_service.refresh_source(source_id, triggered_by="admin")
    assert [r.triggered_by for r in await _runs(async_session_maker, source_id)] == ["admin"]


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def _stub_session(dialect: str | None):
    """A stand-in shaped like the only two things `_supports_concurrent_sessions`
    looks at: `.bind`, and `.bind.dialect.name`. `dialect=None` is the unbound
    case, which is what a real `AsyncSession` reports when it was built from a
    sessionmaker it no longer holds a bind for."""

    class _Session:
        bind = (
            None
            if dialect is None
            else type("_Bind", (), {"dialect": type("_Dialect", (), {"name": dialect})()})()
        )

    return _Session()


def test_concurrency_is_refused_on_a_single_connection_database() -> None:
    # SQLite here is served by one shared connection, where two sessions'
    # statements interleave and two transactions quietly become one. Running the
    # sweep concurrently there loses commits without raising anything.
    assert lifecycle_service._supports_concurrent_sessions(_stub_session("sqlite")) is False


def test_concurrency_is_allowed_on_postgres() -> None:
    assert lifecycle_service._supports_concurrent_sessions(_stub_session("postgresql")) is True


def test_concurrency_is_refused_when_there_is_no_bind() -> None:
    # A session bound to nothing has no dialect to reason about, and guessing
    # "probably fine" here would produce concurrent sessions over an unknown
    # engine. Degrading to serial is the safe direction to be wrong in.
    assert lifecycle_service._supports_concurrent_sessions(_stub_session(None)) is False


async def test_every_due_source_is_ingested_when_there_are_more_than_the_fan_out(
    async_session_maker, category
) -> None:
    # On the test database the sweep runs serially, so this also guards the
    # ordering contract the concurrent path depends on: results line up with the
    # targets they belong to, and `strict=True` would raise if they ever did not.
    count = lifecycle_service.MAX_CONCURRENT_SOURCES + 3
    source_ids = [
        await _add_source(
            async_session_maker,
            slug=f"s{index:02d}",
            offers=[_offer(f"shop{index:02d}")],
        )
        for index in range(count)
    ]

    async with async_session_maker() as db:
        assert await refresh_coupon_prices(db) == count

    for source_id in source_ids:
        assert [r.status for r in await _runs(async_session_maker, source_id)] == [
            RunStatus.success
        ]
    assert len(await _coupon_titles(async_session_maker)) == count


# ---------------------------------------------------------------------------
# Scheduler wiring
# ---------------------------------------------------------------------------


async def test_the_manual_refresh_path_can_force_every_source(
    async_session_maker, category
) -> None:
    await _add_source(
        async_session_maker,
        slug="fresh",
        offers=[_offer("freshshop")],
        last_sync_at=datetime.now(UTC),
    )
    assert "refreshed 1" in await scheduler.run_job_now("refresh_prices", force=True)


async def test_the_scheduled_refresh_path_respects_the_intervals(
    async_session_maker, category
) -> None:
    await _add_source(
        async_session_maker,
        slug="fresh",
        offers=[_offer("freshshop")],
        last_sync_at=datetime.now(UTC),
    )
    assert "refreshed 0" in await scheduler.run_job_now("refresh_prices")


# ---------------------------------------------------------------------------
# The adapter registry
# ---------------------------------------------------------------------------


def test_every_source_kind_is_either_implemented_or_declared_pending() -> None:
    # The failure this prevents: someone adds `SourceKind.rakuten`, creates a
    # row, and every run fails for a reason that says nothing about which of the
    # two cases it was.
    accounted = set(registry._ADAPTERS) | set(registry.PENDING_KINDS)
    assert accounted == set(SourceKind)


def test_an_implemented_kind_resolves_to_an_adapter() -> None:
    adapter = registry.get_adapter(SourceKind.awin)
    assert callable(adapter.fetch)


def test_a_pending_kind_says_it_is_pending() -> None:
    with pytest.raises(ValueError, match="no adapter yet"):
        registry.get_adapter(SourceKind.cj)


def test_an_unknown_kind_is_reported_as_a_missing_adapter() -> None:
    # Deliberately not the pending message: a kind that is not in the enum at all
    # is a bug in the caller, and telling the operator to go write an adapter for
    # it would send them somewhere wrong.
    with pytest.raises(ValueError, match="No adapter registered"):
        registry.get_adapter("not-a-kind")


def test_a_registered_kind_is_never_also_pending() -> None:
    # Belonging to both sets would mean the `if kind in PENDING_KINDS` branch is
    # unreachable for it, which is fine — until somebody adds an adapter without
    # removing it and the message starts lying.
    assert set(registry._ADAPTERS).isdisjoint(registry.PENDING_KINDS)
