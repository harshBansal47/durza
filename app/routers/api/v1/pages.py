import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_role
from app.core.pagination import Paginated
from app.models.page import Page
from app.models.user import Role, User
from app.schemas.page import PageCreate, PageRead, PageUpdate
from app.services import page_service

router = APIRouter(prefix="/pages", tags=["pages"])


@router.get("", response_model=Paginated[PageRead])
async def list_pages(
    published_only: bool = True,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> Paginated[PageRead]:
    items, total = await page_service.list_pages(
        db, published_only=published_only, skip=skip, limit=limit
    )
    return Paginated(
        items=[PageRead.model_validate(item) for item in items],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get("/by-slug/{slug}", response_model=PageRead)
async def get_page_by_slug(slug: str, db: AsyncSession = Depends(get_db)) -> Page:
    page = await page_service.get_page_by_slug(db, slug)
    if page is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page not found")
    return page


@router.get("/{page_id}", response_model=PageRead)
async def get_page(page_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> Page:
    page = await page_service.get_page(db, page_id)
    if page is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page not found")
    return page


@router.post("", response_model=PageRead, status_code=status.HTTP_201_CREATED)
async def create_page(
    payload: PageCreate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role(Role.admin, Role.editor)),
) -> Page:
    return await page_service.create_page(db, payload)


@router.patch("/{page_id}", response_model=PageRead)
async def update_page(
    page_id: uuid.UUID,
    payload: PageUpdate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role(Role.admin, Role.editor)),
) -> Page:
    page = await page_service.get_page(db, page_id)
    if page is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page not found")
    return await page_service.update_page(db, page, payload)


@router.delete("/{page_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_page(
    page_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role(Role.admin)),
) -> None:
    page = await page_service.get_page(db, page_id)
    if page is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page not found")
    await page_service.delete_page(db, page)
