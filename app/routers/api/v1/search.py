"""Search type-ahead in one request.

The storefront's search box used to call `/stores/autocomplete`, `/categories/autocomplete`
and `/products/autocomplete` for every query: three HTTP requests, three pooled database
connections, three passes through the middleware stack. This answers all three at once.
The individual endpoints remain, and the response matches what the client assembled from them.
"""

import logging
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.services import category_service, price_service, store_service

router = APIRouter(prefix="/search", tags=["search"])
logger = logging.getLogger(__name__)


class SuggestRead(BaseModel):
    stores: list[str]
    categories: list[str]
    products: list[str]


async def _safely(
    db: AsyncSession, name: str, fetch: Callable[[], Awaitable[list[str]]]
) -> list[str]:
    """One source failing must not blank the others, as with the three separate calls."""
    try:
        return await fetch()
    except Exception:
        logger.warning("search suggest: %s lookup failed", name, exc_info=True)
        await db.rollback()  # the session must be usable for the next lookup
        return []


@router.get("/suggest", response_model=SuggestRead)
async def suggest(
    q: str = Query(..., min_length=1, max_length=100),
    limit: int = Query(5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
) -> SuggestRead:
    # One session cannot run concurrent statements, so these run in sequence; each is a
    # single small indexed lookup, and that is still one connection instead of three.
    return SuggestRead(
        stores=await _safely(db, "stores", lambda: store_service.autocomplete_stores(db, q, limit)),
        categories=await _safely(
            db, "categories", lambda: category_service.autocomplete_categories(db, q, limit)
        ),
        products=await _safely(
            db, "products", lambda: price_service.autocomplete_products(db, q, limit)
        ),
    )
