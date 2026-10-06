import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import paginate
from app.core.slugs import generate_unique_slug
from app.models.store import Store
from app.schemas.store import StoreCreate, StoreUpdate


async def list_stores(
    db: AsyncSession,
    *,
    search: str | None = None,
    country_code: str | None = None,
    skip: int = 0,
    limit: int = 20,
) -> tuple[Sequence[Store], int]:
    """List stores, optionally scoped to one market.

    `country_code` is the per-country listing filter Savevero needs to be
    indexable in each market it operates in. It also matches global stores
    (those with no country of their own) so they don't vanish from a
    country-filtered view.
    """
    stmt = select(Store)
    if search:
        stmt = stmt.where(Store.name.ilike(f"%{search}%"))
    if country_code:
        code = country_code.strip().upper()
        stmt = stmt.where((Store.country_code == code) | (Store.country_code.is_(None)))
    return await paginate(db, stmt.order_by(Store.name), skip, limit)


async def get_store(db: AsyncSession, store_id: uuid.UUID) -> Store | None:
    return await db.get(Store, store_id)


async def get_store_by_slug(db: AsyncSession, slug: str) -> Store | None:
    result = await db.execute(select(Store).where(Store.slug == slug))
    return result.scalar_one_or_none()


async def create_store(db: AsyncSession, payload: StoreCreate) -> Store:
    store = Store(
        name=payload.name,
        slug=await generate_unique_slug(db, Store, payload.name),
        logo_url=payload.logo_url,
        website_url=payload.website_url,
        description=payload.description,
        commission_disclosure=payload.commission_disclosure,
        country_code=payload.country_code,
        currency=payload.currency,
        affiliate_network=payload.affiliate_network or "none",
        link_template=payload.link_template,
        cookie_days=payload.cookie_days,
    )
    db.add(store)
    await db.commit()
    await db.refresh(store)
    return store


async def update_store(db: AsyncSession, store: Store, payload: StoreUpdate) -> Store:
    data = payload.model_dump(exclude_unset=True)
    if "name" in data and data["name"] != store.name:
        data["slug"] = await generate_unique_slug(db, Store, data["name"], exclude_id=store.id)
    # The column is NOT NULL; an explicit null from a client means "reset".
    if "affiliate_network" in data and data["affiliate_network"] is None:
        data["affiliate_network"] = "none"
    for field, value in data.items():
        setattr(store, field, value)
    await db.commit()
    await db.refresh(store)
    return store


async def delete_store(db: AsyncSession, store: Store) -> None:
    await db.delete(store)
    await db.commit()


async def autocomplete_stores(db: AsyncSession, query: str, limit: int = 10) -> list[str]:
    result = await db.execute(
        select(Store.name).where(Store.name.ilike(f"%{query}%")).order_by(Store.name).limit(limit)
    )
    return list(result.scalars().all())


async def list_store_countries(db: AsyncSession) -> list[str]:
    """Every market that currently has at least one country-specific store."""
    result = await db.execute(
        select(Store.country_code)
        .where(Store.country_code.is_not(None), Store.is_active.is_(True))
        .group_by(Store.country_code)
        .order_by(func.count().desc())
    )
    return [code for code in result.scalars().all() if code]
