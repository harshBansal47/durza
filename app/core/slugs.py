import uuid
from typing import Any

from slugify import slugify
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def generate_unique_slug(
    db: AsyncSession,
    model: Any,
    base_text: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> str:
    """Slugify base_text, appending -2, -3, ... until unique for this model."""
    base_slug = slugify(base_text)
    slug = base_slug
    counter = 1
    while True:
        stmt = select(model).where(model.slug == slug)
        if exclude_id is not None:
            stmt = stmt.where(model.id != exclude_id)
        existing = (await db.execute(stmt)).scalar_one_or_none()
        if existing is None:
            return slug
        counter += 1
        slug = f"{base_slug}-{counter}"
