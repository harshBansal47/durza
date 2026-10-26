import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_role
from app.core.pagination import Paginated
from app.models.editorial import EditorialContent, EditorialEntityLink, StoreFAQ, StoreRelationship
from app.models.store import Store
from app.models.user import Role, User
from app.schemas.coupon_public import CouponPublicRead
from app.schemas.destination import StoreDestinationRead, StoreFAQRead
from app.schemas.editorial import EditorialRead
from app.schemas.store import StoreCreate, StoreRead, StoreUpdate
from app.services import coupon_service, editorial_service, store_service

router = APIRouter(prefix="/stores", tags=["stores"])


@router.get("/by-slug/{slug}/destination", response_model=StoreDestinationRead)
async def get_store_destination(
    slug: str, db: AsyncSession = Depends(get_db)
) -> StoreDestinationRead:
    """Aggregate the existing merchant, offer, and editorial records for a store destination."""
    store = await store_service.get_store_by_slug(db, slug)
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Store not found")

    # Two buckets that together cover every live offer: the ones with a code,
    # and the ones without. (This used to ask for `discount_type=deal` as well,
    # so a "20% off, no code needed" offer matched neither query and vanished
    # from the store page while still being counted in its total.)
    deals, _ = await coupon_service.list_coupons(db, store_id=store.id, has_code=False, limit=100)
    coupons, _ = await coupon_service.list_coupons(db, store_id=store.id, has_code=True, limit=100)
    faq_result = await db.scalars(
        select(StoreFAQ).where(StoreFAQ.store_id == store.id).order_by(StoreFAQ.sort_order)
    )
    alternative_result = await db.scalars(
        select(Store)
        .join(StoreRelationship, StoreRelationship.related_store_id == Store.id)
        .where(StoreRelationship.store_id == store.id, Store.is_active.is_(True))
        .order_by(StoreRelationship.sort_order, Store.name)
    )
    content_result = await db.scalars(
        select(EditorialContent)
        .join(EditorialEntityLink, EditorialEntityLink.content_id == EditorialContent.id)
        .where(
            EditorialEntityLink.store_id == store.id,
            EditorialContent.status == "published",
            EditorialContent.is_indexable.is_(True),
        )
        .order_by(EditorialContent.published_at.desc())
        .limit(12)
    )
    content_items = list(content_result)
    content_reads = []
    for item in content_items:
        relations, links, faqs = await editorial_service.related_data(db, item.id)
        content_reads.append(
            EditorialRead.model_validate(
                {
                    **{
                        field: getattr(item, field)
                        for field in EditorialRead.model_fields
                        if field not in {"relations", "entity_links", "faqs", "adsense_eligible"}
                    },
                    "relations": relations,
                    "entity_links": links,
                    "faqs": faqs,
                }
            )
        )
    return StoreDestinationRead(
        store=StoreRead.model_validate(store),
        deals=[CouponPublicRead.model_validate(item) for item in deals],
        coupons=[CouponPublicRead.model_validate(item) for item in coupons],
        faqs=[StoreFAQRead.model_validate(item) for item in faq_result],
        alternatives=[StoreRead.model_validate(item) for item in alternative_result],
        related_content=content_reads,
    )


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
    counts = await store_service.live_offer_counts(db, [item.id for item in items])
    return Paginated(
        items=[
            StoreRead.model_validate(item).model_copy(update={"offer_count": counts.get(item.id, 0)})
            for item in items
        ],
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
    try:
        return await store_service.create_store(db, payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


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
    try:
        return await store_service.update_store(db, store, payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


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
