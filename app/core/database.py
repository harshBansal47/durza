from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

settings = get_settings()

# ---- Pool configuration ------------------------------------------------------
# Production talks to Neon (or any managed Postgres) over the public internet,
# and that changes what "a connection in the pool" means:
#
#   * Idle-connection reaping. Neon's pooler closes connections after a few
#     idle minutes (and Neon suspends compute entirely). A pool built with
#     SQLAlchemy's defaults has no idea: it hands the dead socket to the next
#     request and asyncpg fails with `connection is closed` — a 500 for
#     whoever happened to arrive after the quiet period (`pool_pre_ping` fixes
#     this by probing and transparently replacing dead connections).
#   * Sockets that died between the probe and first use (`pool_recycle` retires
#     everything older than the server-side idle window).
#
# `pool_use_lifo` hands out the most-recently-used connection first, which keeps
# the number of concurrently open connections (and the DB's memory) down.
#
# SQLite (the test suite) keeps the library defaults: StaticPool for `:memory:`
# rejects pool_size/max_overflow, and aiosqlite does not accept asyncpg's
# connect_args.
_is_postgres = settings.database_url.startswith(("postgresql", "postgres"))

_engine_options: dict = {
    "echo": settings.environment == "development",
    "pool_pre_ping": True,
    "pool_recycle": 300,
}
if _is_postgres:
    _engine_options.update(
        pool_size=5,
        # Bounded overflow: one overloaded page fan-out should queue, not open
        # a connection per request and exhaust the server's connection limit.
        max_overflow=5,
        pool_timeout=30,
        pool_use_lifo=True,
        connect_args={
            # Fail fast instead of hanging: 15s to establish a connection, 60s
            # for any single statement. Both surface as ordinary errors the
            # caller can retry; without them a network partition parks requests
            # until the client gives up.
            "timeout": 15,
            "command_timeout": 60,
        },
    )

engine = create_async_engine(settings.database_url, **_engine_options)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    """Shared declarative base — every ORM model inherits from this."""


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a DB session per request."""
    async with AsyncSessionLocal() as session:
        yield session
