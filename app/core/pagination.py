from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select


class Paginated[T](BaseModel):
    """Standard list-endpoint envelope: items plus enough to page through them."""

    items: list[T]
    total: int
    skip: int
    limit: int


async def paginate(
    db: AsyncSession, stmt: Select, skip: int = 0, limit: int = 20
) -> tuple[Sequence[Any], int]:
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    result = await db.execute(stmt.offset(skip).limit(limit))
    items: Sequence[Any] = result.scalars().all()
    return items, total or 0
