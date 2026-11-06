import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_role
from app.core.limiter import limiter
from app.core.pagination import Paginated
from app.core.request_meta import client_ip, hash_ip
from app.models.editorial import EditorialContent, EditorialEntityLink, StoreFAQ, StoreRelationship
from app.models.store import Store
from app.models.user import Role, User
from app.schemas.coupon_public import CouponPublicRead
from app.schemas.destination import StoreDestinationRead, StoreFAQRead
from app.schemas.editorial import EditorialRead
from app.schemas.store import StoreCreate, StoreRead, StoreUpdate
from app.services import (
    affiliate,
    click_service,
    coupon_service,
    editorial_service,
    store_router,
    store_service,
)

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
    deals, deals_total = await coupon_service.list_coupons(
        db, store_id=store.id, has_code=False, limit=100
    )
    coupons, coupons_total = await coupon_service.list_coupons(
        db, store_id=store.id, has_code=True, limit=100
    )
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
    related = await editorial_service.related_data_many(db, [item.id for item in content_items])
    for item in content_items:
        relations, links, faqs = related[item.id]
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
    alternative_items = list(alternative_result)
    alt_counts = await store_service.live_offer_counts(db, [item.id for item in alternative_items])
    return StoreDestinationRead(
        store=StoreRead.model_validate(store),
        deals=[CouponPublicRead.model_validate(item) for item in deals],
        coupons=[CouponPublicRead.model_validate(item) for item in coupons],
        faqs=[StoreFAQRead.model_validate(item) for item in faq_result],
        alternatives=[
            StoreRead.model_validate(item).model_copy(
                update={"offer_count": alt_counts.get(item.id, 0)}
            )
            for item in alternative_items
        ],
        related_content=content_reads,
        offer_total=deals_total + coupons_total,
    )


@router.get("", response_model=Paginated[StoreRead])
async def list_stores(
    search: str | None = Query(None, max_length=100),
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
            StoreRead.model_validate(item).model_copy(
                update={"offer_count": counts.get(item.id, 0)}
            )
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


@router.get(
    "/by-slug/{slug}/go", response_class=RedirectResponse, status_code=status.HTTP_302_FOUND
)
@limiter.limit("60/minute")
async def go_to_store(
    slug: str,
    request: Request,
    src: str | None = Query(default=None, max_length=40),
    content_ref: str | None = Query(default=None, max_length=80),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """ "Go to store": the redirect behind every store link on the site and in Telegram.

    The destination is chosen here, server-side, by `app.services.store_router`; the browser
    never sees which network or campaign it was. Each hit is logged as a click tagged with
    `src` (the page or channel that sent the visitor), and the outbound URL carries that click's
    reference where the network supports one.
    """
    store = await store_service.get_store_by_slug(db, slug)
    if store is None or not store.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Store not found")
    routed = await store_router.route_store(db, store, src=src)
    if routed is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Store has no destination"
        )
    click, _ = await click_service.record_click(
        db,
        store=store,
        src=src,
        content_ref=content_ref,
        network=routed.network,
        visitor_hash=hash_ip(client_ip(request)),
        user_agent=request.headers.get("user-agent"),
    )
    target = affiliate.build_outbound_url(
        routed.url,
        network=routed.network,
        link_template=routed.link_template,
        clickref=click.clickref,
    )
    return RedirectResponse(target, status_code=status.HTTP_302_FOUND)


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
