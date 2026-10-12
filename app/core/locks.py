"""Postgres advisory locks: taking one, and giving it back.

An advisory lock is how N replicas of this process agree on which of them does a
piece of work. Two properties matter and both are easy to get wrong:

* **Session-scoped, not transaction-scoped.** `pg_try_advisory_xact_lock` is
  released the instant the transaction that took it commits or rolls back. Work
  that commits more than once — which ingestion does, once per batch — would
  therefore be unguarded from its second commit onwards. `pg_try_advisory_lock`
  is held until it is explicitly unlocked or the connection closes.
* **Explicitly released, always.** SQLAlchemy returns a connection to the pool
  when a session closes, and a pool rollback does *not* release advisory locks.
  A leaked lock is held by a connection that is now serving somebody else's
  request, so every later attempt reports "another worker held the lock" for as
  long as the process lives. Callers therefore release in a `finally`.

These live here rather than inside the scheduler because per-source ingestion
needs the identical two functions. Two implementations of "how do we take a
lock" is two chances for one of them to forget the `finally`, and the symptom of
forgetting it is a job that works perfectly on one replica and silently skips
forever on another.
"""

import hashlib

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.observability import get_logger

logger = get_logger("durza.locks")

#: Set in every id produced by `source_lock_id`, which makes the result negative
#: once read as a signed 64-bit integer. The scheduler's job locks are all small
#: positive constants, so the two namespaces are provably disjoint.
#:
#: This is worth a bit rather than leaving to chance: a collision would not fail
#: loudly, it would make an unrelated pair of operations serialise against each
#: other, and the resulting "another worker held the lock" messages would point
#: at the scheduler rather than at the hash.
_SOURCE_LOCK_BIT = 1 << 63


async def try_lock(db: AsyncSession, lock_id: int) -> bool:
    """Take a session-scoped advisory lock. Caller must release it.

    Returns True when the caller may proceed. Advisory locks are a Postgres
    feature, so on any other dialect (the test suite runs SQLite) this returns
    True unconditionally: a single shared test connection cannot have two workers
    racing in the first place, and a hard failure here would mean the locking is
    untestable rather than wrong.
    """
    if db.bind is not None and db.bind.dialect.name != "postgresql":
        return True
    result = await db.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": lock_id})
    # Commit so the lock is held by this connection rather than being discarded
    # with the implicit transaction's result set.
    await db.commit()
    return bool(result.scalar())


async def unlock(db: AsyncSession, lock_id: int) -> None:
    """Release a lock taken by `try_lock`. Never raises.

    If the connection is already gone, Postgres released the lock when the
    backend exited. Nothing to do, and nothing worth failing the job over — the
    worst case is a log line that says so.
    """
    if db.bind is not None and db.bind.dialect.name != "postgresql":
        return
    try:
        await db.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": lock_id})
        await db.commit()
    except SQLAlchemyError:
        logger.warning("advisory lock release failed", extra={"lock_id": lock_id})


def source_lock_id(source_id: object) -> int:
    """The advisory-lock id for one source, derived from its primary key.

    Derived from the row's own identity rather than from an enum, a counter, or
    the position in a list of sources, because the lock is only meaningful if
    every process computes the same value for the same row. A scheme that
    handed out ids in discovery order would let two replicas lock different
    sources at the same moment and both call that mutual exclusion.

    `hash()` is unusable for this: Python randomises string hashing per process,
    so two replicas would derive different ids from the same UUID and the lock
    would protect nothing while still looking like it worked.

    UUIDs are already uniformly distributed, so any stable mixing function is
    fine — `blake2b` is used because it is stable across processes and releases
    (unlike the builtin `hash`) and because it cannot raise on the short inputs
    that a `zlib.crc32` would collide on.
    """
    digest = hashlib.blake2b(str(source_id).encode(), digest_size=8).digest()
    return (int.from_bytes(digest, "big") | _SOURCE_LOCK_BIT) - (1 << 64)


__all__ = ["source_lock_id", "try_lock", "unlock"]