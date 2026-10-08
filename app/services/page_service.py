import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import paginate
from app.core.slugs import generate_unique_slug
from app.models.page import Page
from app.schemas.page import PageCreate, PageUpdate


async def list_pages(
    db: AsyncSession, *, published_only: bool = True, skip: int = 0, limit: int = 20
) -> tuple[Sequence[Page], int]:
    stmt = select(Page)
    if published_only:
        stmt = stmt.where(Page.is_published.is_(True))
    return await paginate(db, stmt.order_by(Page.title), skip, limit)


async def get_page(db: AsyncSession, page_id: uuid.UUID) -> Page | None:
    return await db.get(Page, page_id)


async def get_page_by_slug(db: AsyncSession, slug: str) -> Page | None:
    result = await db.execute(select(Page).where(Page.slug == slug))
    return result.scalar_one_or_none()


async def create_page(db: AsyncSession, payload: PageCreate) -> Page:
    page = Page(
        title=payload.title,
        slug=await generate_unique_slug(db, Page, payload.title),
        content=payload.content,
        meta_description=payload.meta_description,
        is_published=payload.is_published,
    )
    db.add(page)
    await db.commit()
    await db.refresh(page)
    return page


async def update_page(db: AsyncSession, page: Page, payload: PageUpdate) -> Page:
    data = payload.model_dump(exclude_unset=True)
    if "title" in data and data["title"] != page.title:
        data["slug"] = await generate_unique_slug(db, Page, data["title"], exclude_id=page.id)
    for field, value in data.items():
        setattr(page, field, value)
    await db.commit()
    await db.refresh(page)
    return page


async def delete_page(db: AsyncSession, page: Page) -> None:
    await db.delete(page)
    await db.commit()
