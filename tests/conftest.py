import os
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import Settings

# The suite must not read the developer's `.env`, and this has to happen before
# any `get_settings()` call, which is why it sits above the app imports.
#
# Without it, `.env` decides how the tests behave: a developer's REDIS_URL sends
# the rate limiter looking for a Redis that is not running, and a SENTRY_DSN or
# SECRET_KEY in `.env` silently changes what is under test. A test that passes
# on one machine and fails on another for that reason is the worst kind of flake,
# because it looks like a real bug and is not.
Settings.model_config["env_file"] = None
os.environ["ENVIRONMENT"] = "test"
os.environ.pop("REDIS_URL", None)
os.environ.pop("SENTRY_DSN", None)

# `database_url` is a required setting with no default, and until now it was
# being satisfied by `.env` — which is to say the module-level engine in
# `app.core.database` was pointed at the developer's real Postgres. API tests
# never noticed, because `client` overrides `get_db`; anything that used the
# module-level `AsyncSessionLocal` (the scheduler, the scripts) was writing to
# the development database while the suite reported success.
#
# Pointing it at the same throwaway database the fixtures use closes that.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

# The module-level engine in `app.core.database` is built from `DATABASE_URL` at
# import time, which is now the throwaway test URL — but without the schema
# `search_path` that `_build_engine` applies. Left alone, a developer running
# the suite against a shared Postgres with `TEST_DATABASE_SCHEMA` set would have
# the fixtures writing to an isolated schema while every code path that used the
# module-level `AsyncSessionLocal` wrote to `public`, i.e. to their real data.
#
# Replacing both attributes is enough: `get_db` reads `AsyncSessionLocal` off the
# module at call time, so the override takes effect without touching the
# function.
import app.core.database as _database
from app.core.database import Base, get_db
from app.core.limiter import limiter
from app.core.security import create_access_token
from app.main import app

# Ensure every model is registered on Base.metadata before create_all runs.
from app.models import ad as _ad  # noqa: F401
from app.models import affiliate_destination as _affiliate_destination  # noqa: F401
from app.models import affiliate_program as _affiliate_program  # noqa: F401
from app.models import affiliate_provider as _affiliate_provider  # noqa: F401
from app.models import affiliate_revenue as _affiliate_revenue  # noqa: F401
from app.models import analytics_session as _analytics_session  # noqa: F401
from app.models import attribution as _attribution  # noqa: F401
from app.models import category as _category  # noqa: F401
from app.models import click as _click  # noqa: F401
from app.models import conversion as _conversion  # noqa: F401
from app.models import coupon as _coupon  # noqa: F401
from app.models import coupon_impression as _coupon_impression  # noqa: F401
from app.models import coupon_source as _coupon_source  # noqa: F401
from app.models import coupon_verification as _coupon_verification  # noqa: F401
from app.models import daily_stats as _daily_stats  # noqa: F401
from app.models import ingestion_run as _ingestion_run  # noqa: F401
from app.models import job as _job  # noqa: F401
from app.models import page as _page  # noqa: F401
from app.models import product as _product  # noqa: F401
from app.models import revenue_event as _revenue_event  # noqa: F401
from app.models import source as _source  # noqa: F401
from app.models import sponsored as _sponsored  # noqa: F401
from app.models import store as _store  # noqa: F401
from app.models import tracking as _tracking  # noqa: F401
from app.models import user as _user  # noqa: F401
from app.models import verification_attempt as _verification_attempt  # noqa: F401
from app.models.user import Role
from app.schemas.user import UserCreate
from app.services import user_service

# SQLite in memory by default, because it needs no server and a developer should
# be able to run the suite with one command and no setup.
#
# `TEST_DATABASE_URL` overrides it, and CI sets it to Postgres. That is not
# belt-and-braces: SQLite and Postgres disagree on the things this codebase most
# needs tested. Advisory locks do not exist on SQLite at all (so the scheduler
# silently takes the non-Postgres branch and the lock is never exercised), and
# SQLite hands back naive datetimes from `timestamptz` columns and ignores
# `Numeric` precision. A suite that only ever sees SQLite cannot catch a
# production-only failure in either.
#
# `DATABASE_URL` is deliberately *not* read here. This is a throwaway test
# database that gets dropped and recreated; pointing it at a developer's working
# `DATABASE_URL` would truncate their actual data.
#
# `TEST_DATABASE_SCHEMA` exists because a Postgres instance is often shared — a
# CI service container, or a laptop that also has a development database. Creating
# a schema is permitted far more often than creating a database, and a schema is
# isolated the same way for these purposes: `create_all` builds into it and
# `drop_all` removes only what it created.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
TEST_DATABASE_SCHEMA = os.environ.get("TEST_DATABASE_SCHEMA")
USING_POSTGRES = TEST_DATABASE_URL.startswith("postgresql")


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """The limiter's storage is a process-wide singleton; without this, tests
    that hit /register, /login, or /verify enough times would rate-limit
    each other in ways that have nothing to do with what each test is
    actually checking."""
    limiter.reset()
    yield
    limiter.reset()


def _build_engine():
    """The throwaway test database, built once and shared.

    Shared between the `engine` fixture and the module-level engine in
    `app.core.database`, so there is exactly one answer to "which database is
    this test talking to". Two engines over one in-memory SQLite are two separate
    databases, and a test that passed because of that would be passing for a
    reason that has nothing to do with the code under test.
    """
    if USING_POSTGRES:
        # No StaticPool: a single connection would serialise every test and, more
        # importantly, a shared connection cannot hold two sessions at once, which
        # is exactly the situation the advisory-lock tests need.
        connect_args: dict = {}
        if TEST_DATABASE_SCHEMA:
            connect_args["server_settings"] = {"search_path": TEST_DATABASE_SCHEMA}
        return create_async_engine(TEST_DATABASE_URL, connect_args=connect_args)

    engine = create_async_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # SQLite ignores foreign keys unless told otherwise; Postgres always enforces
    # them. Registered on the sync engine so it applies to every pooled
    # connection rather than just the first.
    @event.listens_for(engine.sync_engine, "connect")
    def _enable_fk(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


# The module-level engine in `app.core.database` is built from `DATABASE_URL` at
# import time, which is now the throwaway test URL — but without the schema
# `search_path` that `_build_engine` applies. Left alone, a developer running
# the suite against a shared Postgres with `TEST_DATABASE_SCHEMA` set would have
# the fixtures writing to an isolated schema while every code path that used the
# module-level `AsyncSessionLocal` wrote to `public`, i.e. to their real data.
#
# Replacing both attributes is enough: `get_db` reads `AsyncSessionLocal` off the
# module at call time, so the override takes effect without touching the
# function.

_database.engine = _build_engine()
_database.AsyncSessionLocal = async_sessionmaker(_database.engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def engine():
    engine = _build_engine()

    if USING_POSTGRES and TEST_DATABASE_SCHEMA:
        # The schema has to exist before `search_path` points at it, and it is
        # created outside the metadata so `drop_all` below does not try to drop a
        # schema it never created.
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{TEST_DATABASE_SCHEMA}"'))

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    if USING_POSTGRES and TEST_DATABASE_SCHEMA:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{TEST_DATABASE_SCHEMA}" CASCADE'))
    elif USING_POSTGRES:
        # Drop rather than truncate: enum types created by `create_all` are not
        # removed by `DROP TABLE`, and leaving them behind makes the next run fail
        # with "type already exists".
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def async_session_maker(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def client(async_session_maker):
    async def _override_get_db():
        async with async_session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def make_headers(async_session_maker):
    """Factory: `await make_headers(Role.admin)` -> {"Authorization": "Bearer ..."}."""

    async def _make(role: Role = Role.user) -> dict[str, str]:
        email = f"{role.value}-{uuid.uuid4().hex[:8]}@example.com"
        async with async_session_maker() as session:
            user = await user_service.create_user(
                session, UserCreate(email=email, password="supersecret123"), role=role
            )
            token = create_access_token(str(user.id), user.role.value)
        return {"Authorization": f"Bearer {token}"}

    return _make


@pytest_asyncio.fixture
async def admin_headers(make_headers):
    return await make_headers(Role.admin)


@pytest_asyncio.fixture
async def mcp_client(async_session_maker):
    """A BachatBaseClient pointed at the same in-memory test app, via ASGITransport —
    no real network needed, same pattern as the `client` fixture above."""
    from httpx import ASGITransport

    from mcp_server.client import BachatBaseClient

    async def _override_get_db():
        async with async_session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    yield BachatBaseClient(base_url="http://testserver/api/v1", transport=ASGITransport(app=app))
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def editor_headers(make_headers):
    return await make_headers(Role.editor)


@pytest_asyncio.fixture
async def user_headers(make_headers):
    return await make_headers(Role.user)
