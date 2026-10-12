import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_role
from app.core.pagination import Paginated
from app.models.store import Store
from app.models.user import Role, User
from app.schemas.store import StoreCreate, StoreRead, StoreUpdate
from app.services import store_service

router = APIRouter(prefix="/stores", tags=["stores"])


@router.get("", response_model=Paginated[StoreRead])
async def list_stores(
    search: str | None = None,
    country_code: str | None = Query(
        None,
        min_length=2,
        max_length=2,
        description="ISO 3166-1 alpha-2. Scopes results to one market; stores with no "
        "country of their own (global retailers) are always included.",
    ),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> Paginated[StoreRead]:
    items, total = await store_service.list_stores(
        db, search=search, country_code=country_code, skip=skip, limit=limit
    )
    return Paginated(
        items=[StoreRead.model_validate(item) for item in items],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get("/by-slug/{slug}", response_model=StoreRead)
async def get_store_by_slug(slug: str, db: AsyncSession = Depends(get_db)) -> Store:
    store = await store_service.get_store_by_slug(db, slug)
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Store not found")
    return store


@router.get("/markets", response_model=list[str])
async def list_markets(db: AsyncSession = Depends(get_db)) -> list[str]:
    """ISO 3166-1 alpha-2 codes that have at least one active store.

    Drives the country switcher. Public on purpose: knowing which markets
    Durza actually serves is the point of the page.
    """
    return await store_service.list_store_countries(db)


@router.get("/autocomplete", response_model=list[str])
async def autocomplete_stores(
    q: str = Query(..., min_length=1, max_length=100),
    limit: int = Query(10, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
) -> list[str]:
    """Return store names matching the query for search autocomplete."""
    return await store_service.autocomplete_stores(db, q, limit)


@router.get("/{store_id}", response_model=StoreRead)
async def get_store(store_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> Store:
    store = await store_service.get_store(db, store_id)
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Store not found")
    return store


@router.post("", response_model=StoreRead, status_code=status.HTTP_201_CREATED)
async def create_store(
    payload: StoreCreate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role(Role.admin, Role.editor)),
) -> Store:
    return await store_service.create_store(db, payload)


@router.patch("/{store_id}", response_model=StoreRead)
async def update_store(
    store_id: uuid.UUID,
    payload: StoreUpdate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role(Role.admin, Role.editor)),
) -> Store:
    store = await store_service.get_store(db, store_id)
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Store not found")
    return await store_service.update_store(db, store, payload)


@router.delete("/{store_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_store(
    store_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role(Role.admin)),
) -> None:
    store = await store_service.get_store(db, store_id)
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Store not found")
    await store_service.delete_store(db, store)
