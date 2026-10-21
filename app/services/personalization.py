"""Personalization data-retention helpers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_interest import UserInterest


async def purge_expired_interests(db: AsyncSession, user_id: uuid.UUID | None = None) -> int:
    """Delete expired interest rows, optionally scoped to one user.

    Returns the number of rows removed. Called on read/sync so an expired row
    cannot be exported or used even before a periodic job runs.
    """
    stmt = delete(UserInterest).where(UserInterest.expires_at <= datetime.now(UTC))
    if user_id is not None:
        stmt = stmt.where(UserInterest.user_id == user_id)
    result = await db.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0)
