"""Price/deal intelligence: record observations, keep rolling lows fresh, flag drops."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.exceptions import EntityNotFoundError
from app.core.pagination import paginate
from app.core.slugs import generate_unique_slug
from app.models.category import Category
from app.models.coupon import Coupon, DiscountType
from app.models.product import PricePoint, Product
from app.models.store import Store
from app.schemas.product import PricePointCreate, ProductCreate, ProductUpdate


async def list_products(
    db: AsyncSession,
    *,
    store_id: uuid.UUID | None = None,
    category_id: uuid.UUID | None = None,
    search: str | None = None,
    skip: int = 0,
    limit: int = 20,
) -> tuple[Sequence[Product], int]:
    stmt = select(Product)
    if store_id is not None:
        stmt = stmt.where(Product.store_id == store_id)
    if category_id is not None:
        stmt = stmt.where(Product.category_id == category_id)
    if search:
        stmt = stmt.where(Product.name.ilike(f"%{search}%"))
    return await paginate(db, stmt.order_by(Product.created_at.desc()), skip, limit)


async def get_product(db: AsyncSession, product_id: uuid.UUID) -> Product | None:
    return await db.get(Product, product_id)


async def get_product_by_slug(db: AsyncSession, slug: str) -> Product | None:
    result = await db.execute(select(Product).where(Product.slug == slug))
    return result.scalar_one_or_none()


async def price_history(
    db: AsyncSession, product_id: uuid.UUID, *, limit: int = 200
) -> Sequence[PricePoint]:
    result = await db.execute(
        select(PricePoint)
        .where(PricePoint.product_id == product_id)
        .order_by(PricePoint.captured_at.desc())
        .limit(limit)
    )
    return result.scalars().all()


async def _ensure_store_and_category(
    db: AsyncSession, store_id: uuid.UUID, category_id: uuid.UUID
) -> None:
    if await db.get(Store, store_id) is None:
        raise EntityNotFoundError(f"Store {store_id} not found")
    if await db.get(Category, category_id) is None:
        raise EntityNotFoundError(f"Category {category_id} not found")


async def create_product(db: AsyncSession, payload: ProductCreate) -> Product:
    await _ensure_store_and_category(db, payload.store_id, payload.category_id)
    product = Product(
        name=payload.name,
        slug=await generate_unique_slug(db, Product, payload.name),
        store_id=payload.store_id,
        category_id=payload.category_id,
        url=payload.url,
        image_url=payload.image_url,
        currency=payload.currency,
    )
    db.add(product)
    await db.commit()
    await db.refresh(product)
    return product


async def update_product(db: AsyncSession, product: Product, payload: ProductUpdate) -> Product:
    data = payload.model_dump(exclude_unset=True)
    await _ensure_store_and_category(
        db, data.get("store_id", product.store_id), data.get("category_id", product.category_id)
    )
    if "name" in data and data["name"] != product.name:
        data["slug"] = await generate_unique_slug(db, Product, data["name"], exclude_id=product.id)
    for field, value in data.items():
        setattr(product, field, value)
    await db.commit()
    await db.refresh(product)
    return product


async def delete_product(db: AsyncSession, product: Product) -> None:
    await db.delete(product)
    await db.commit()


def effective_price(price_point: PricePoint, coupon: Coupon | None) -> float:
    """What the product actually costs after the linked coupon (shipping added)."""
    subtotal = float(price_point.price) + float(price_point.shipping or 0)
    if coupon is None or coupon.discount_type == DiscountType.deal:
        return round(subtotal, 2)
    if coupon.discount_type == DiscountType.percentage and coupon.discount_value:
        subtotal -= subtotal * float(coupon.discount_value) / 100
    elif coupon.discount_type == DiscountType.fixed and coupon.discount_value:
        subtotal -= float(coupon.discount_value)
    return round(max(subtotal, 0.0), 2)


async def _lowest_since(db: AsyncSession, product_id: uuid.UUID, since: datetime) -> float | None:
    result = await db.execute(
        select(func.min(PricePoint.price)).where(
            PricePoint.product_id == product_id, PricePoint.captured_at >= since
        )
    )
    return result.scalar_one_or_none()


async def record_price(db: AsyncSession, product: Product, payload: PricePointCreate) -> PricePoint:
    captured = payload.captured_at or datetime.now(UTC)
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=UTC)
    if payload.coupon_id is not None and await db.get(Coupon, payload.coupon_id) is None:
        raise EntityNotFoundError(f"Coupon {payload.coupon_id} not found")

    previous_price = product.current_price
    point = PricePoint(
        product_id=product.id,
        price=payload.price,
        original_price=payload.original_price,
        shipping=payload.shipping,
        in_stock=payload.in_stock,
        coupon_id=payload.coupon_id,
        captured_at=captured,
    )
    db.add(point)
    await db.flush()  # the rolling-low queries below must see this point

    product.current_price = payload.price
    product.list_price = payload.original_price or product.list_price
    product.in_stock = payload.in_stock
    product.last_captured_at = captured

    now = datetime.now(UTC)
    product.lowest_price_7d = await _lowest_since(db, product.id, now - timedelta(days=7))
    product.lowest_price_30d = await _lowest_since(db, product.id, now - timedelta(days=30))
    product.lowest_price_90d = await _lowest_since(db, product.id, now - timedelta(days=90))

    if previous_price is not None and float(payload.price) < float(previous_price):
        product.last_price_drop_pct = round(
            (float(previous_price) - float(payload.price)) / float(previous_price) * 100, 1
        )
        product.last_price_drop_at = captured

    await db.commit()
    await db.refresh(product)
    await db.refresh(point)
    # After the commit, so a rolled-back price does not update the freshness
    # gauge. "When did we last see a price" is a question about stored data, and
    # a point that never landed is not one.
    metrics.mark_price_captured()
    return point


async def autocomplete_products(db: AsyncSession, query: str, limit: int = 10) -> list[str]:
    result = await db.execute(
        select(Product.name)
        .where(Product.name.ilike(f"%{query}%"))
        .order_by(Product.name)
        .limit(limit)
    )
    # `.all()` is typed Sequence[str]; the routers serialise this straight to
    # JSON, so the narrower concrete type is what the contract actually promises.
    return list(result.scalars().all())
