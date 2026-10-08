import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import EntityNotFoundError
from app.core.pagination import paginate
from app.core.slugs import generate_unique_slug
from app.models.category import Category
from app.schemas.category import CategoryCreate, CategoryUpdate


async def list_categories(
    db: AsyncSession, *, parent_id: uuid.UUID | None = None, skip: int = 0, limit: int = 20
) -> tuple[Sequence[Category], int]:
    stmt = select(Category)
    if parent_id is not None:
        stmt = stmt.where(Category.parent_id == parent_id)
    return await paginate(db, stmt.order_by(Category.name), skip, limit)


async def get_category(db: AsyncSession, category_id: uuid.UUID) -> Category | None:
    return await db.get(Category, category_id)


async def get_category_by_slug(db: AsyncSession, slug: str) -> Category | None:
    result = await db.execute(select(Category).where(Category.slug == slug))
    return result.scalar_one_or_none()


async def create_category(db: AsyncSession, payload: CategoryCreate) -> Category:
    if payload.parent_id is not None and await db.get(Category, payload.parent_id) is None:
        raise EntityNotFoundError(f"Parent category {payload.parent_id} not found")

    category = Category(
        name=payload.name,
        slug=await generate_unique_slug(db, Category, payload.name),
        icon=payload.icon,
        parent_id=payload.parent_id,
    )
    db.add(category)
    await db.commit()
    await db.refresh(category)
    return category


async def update_category(
    db: AsyncSession, category: Category, payload: CategoryUpdate
) -> Category:
    data = payload.model_dump(exclude_unset=True)

    parent_id = data.get("parent_id")
    if parent_id is not None:
        if parent_id == category.id:
            raise ValueError("A category cannot be its own parent")
        if await db.get(Category, parent_id) is None:
            raise EntityNotFoundError(f"Parent category {parent_id} not found")

    if "name" in data and data["name"] != category.name:
        data["slug"] = await generate_unique_slug(
            db, Category, data["name"], exclude_id=category.id
        )

    for field, value in data.items():
        setattr(category, field, value)
    await db.commit()
    await db.refresh(category)
    return category


async def delete_category(db: AsyncSession, category: Category) -> None:
    await db.delete(category)
    await db.commit()


async def autocomplete_categories(db: AsyncSession, query: str, limit: int = 10) -> list[str]:
    result = await db.execute(
        select(Category.name)
        .where(Category.name.ilike(f"%{query}%"))
        .order_by(Category.name)
        .limit(limit)
    )
    # `.all()` is typed Sequence[str]; the routers serialise this straight to
    # JSON, so the narrower concrete type is what the contract actually promises.
    return list(result.scalars().all())
