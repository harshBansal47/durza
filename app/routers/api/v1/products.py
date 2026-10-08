import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_role
from app.core.exceptions import EntityNotFoundError
from app.core.pagination import Paginated
from app.core.request_meta import client_ip, hash_ip
from app.models.coupon import Coupon
from app.models.product import Product
from app.models.user import Role, User
from app.schemas.product import (
    PricePointCreate,
    PricePointRead,
    ProductCreate,
    ProductRead,
    ProductUpdate,
)
from app.services import affiliate, click_service, price_service

router = APIRouter(prefix="/products", tags=["products"])


async def _with_effective(db: AsyncSession, product: Product) -> ProductRead:
    read = ProductRead.model_validate(product)
    history = await price_service.price_history(db, product.id, limit=1)
    if history:
        latest = history[0]
        coupon = await db.get(Coupon, latest.coupon_id) if latest.coupon_id else None
        read.effective_price = price_service.effective_price(latest, coupon)
    return read


@router.get("", response_model=Paginated[ProductRead])
async def list_products(
    search: str | None = None,
    store_id: uuid.UUID | None = None,
    category_id: uuid.UUID | None = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> Paginated[ProductRead]:
    items, total = await price_service.list_products(
        db, store_id=store_id, category_id=category_id, search=search, skip=skip, limit=limit
    )
    return Paginated(
        items=[await _with_effective(db, item) for item in items],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get("/by-slug/{slug}", response_model=ProductRead)
async def get_product_by_slug(slug: str, db: AsyncSession = Depends(get_db)) -> ProductRead:
    product = await price_service.get_product_by_slug(db, slug)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return await _with_effective(db, product)


@router.get("/autocomplete", response_model=list[str])
async def autocomplete_products(
    q: str = Query(..., min_length=1, max_length=100),
    limit: int = Query(10, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
) -> list[str]:
    """Return product names matching the query for search autocomplete."""
    return await price_service.autocomplete_products(db, q, limit)


@router.get("/{product_id}", response_model=ProductRead)
async def get_product(product_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> ProductRead:
    product = await price_service.get_product(db, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return await _with_effective(db, product)


@router.get("/{product_id}/go", response_class=RedirectResponse, status_code=status.HTTP_302_FOUND)
async def go_to_product(
    product_id: uuid.UUID,
    request: Request,
    src: str | None = Query(default=None, max_length=40),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Tracked redirect to the store's product page. The raw URL is never in JSON."""
    product = await price_service.get_product(db, product_id)
    if product is None or not product.url:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    store = await click_service.get_store(db, product.store_id)
    click, _ = await click_service.record_click(
        db,
        store=store,
        product=product,
        src=src,
        visitor_hash=hash_ip(client_ip(request)),
        user_agent=request.headers.get("user-agent"),
    )
    target = affiliate.build_outbound_url(
        product.url,
        network=store.affiliate_network if store else None,
        link_template=store.link_template if store else None,
        clickref=click.clickref,
    )
    return RedirectResponse(target, status_code=status.HTTP_302_FOUND)


@router.get("/{product_id}/price-history", response_model=list[PricePointRead])
async def get_price_history(
    product_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> list[PricePointRead]:
    product = await price_service.get_product(db, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    points = await price_service.price_history(db, product_id)
    return [PricePointRead.model_validate(p) for p in points]


@router.post("", response_model=ProductRead, status_code=status.HTTP_201_CREATED)
async def create_product(
    payload: ProductCreate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role(Role.admin, Role.editor)),
) -> ProductRead:
    try:
        product = await price_service.create_product(db, payload)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return await _with_effective(db, product)


@router.patch("/{product_id}", response_model=ProductRead)
async def update_product(
    product_id: uuid.UUID,
    payload: ProductUpdate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role(Role.admin, Role.editor)),
) -> ProductRead:
    product = await price_service.get_product(db, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    try:
        product = await price_service.update_product(db, product, payload)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return await _with_effective(db, product)


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product(
    product_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role(Role.admin)),
) -> None:
    product = await price_service.get_product(db, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    await price_service.delete_product(db, product)


@router.post(
    "/{product_id}/price-points",
    response_model=PricePointRead,
    status_code=status.HTTP_201_CREATED,
)
async def record_price_point(
    product_id: uuid.UUID,
    payload: PricePointCreate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role(Role.admin, Role.editor)),
) -> PricePointRead:
    product = await price_service.get_product(db, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    try:
        point = await price_service.record_price(db, product, payload)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return PricePointRead.model_validate(point)
