import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_role
from app.core.pagination import Paginated
from app.models.editorial import EditorialContent
from app.models.user import Role, User
from app.schemas.editorial import (
    EditorialCreate,
    EditorialHomeRead,
    EditorialRead,
    EditorialStatus,
    EditorialType,
    EditorialUpdate,
)
from app.services import editorial_service

router = APIRouter(prefix="/editorial", tags=["editorial"])


async def _read(db: AsyncSession, content: EditorialContent) -> EditorialRead:
    relations, entity_links, faqs = await editorial_service.related_data(db, content.id)
    return EditorialRead.model_validate(
        {
            **{
                field: getattr(content, field)
                for field in EditorialRead.model_fields
                if field not in {"relations", "entity_links", "faqs", "adsense_eligible"}
            },
            "relations": relations,
            "entity_links": entity_links,
            "faqs": faqs,
        }
    )


@router.get("/homepage", response_model=EditorialHomeRead)
async def get_homepage_editorial(db: AsyncSession = Depends(get_db)) -> EditorialHomeRead:
    """Featured editorial cards for the homepage; offer and category feeds remain separate."""
    sections = {}
    for content_type in EditorialType:
        items, _ = await editorial_service.list_content(
            db, content_type=content_type.value, featured_only=True, limit=6
        )
        sections[content_type.value + "s"] = [await _read(db, item) for item in items]
    return EditorialHomeRead(**sections)


@router.get("", response_model=Paginated[EditorialRead])
async def list_editorial(
    content_type: EditorialType | None = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> Paginated[EditorialRead]:
    items, total = await editorial_service.list_content(
        db, content_type=content_type.value if content_type else None, skip=skip, limit=limit
    )
    return Paginated(
        items=[await _read(db, item) for item in items],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get("/manage", response_model=Paginated[EditorialRead])
async def list_editorial_for_editors(
    content_type: EditorialType | None = None,
    content_status: EditorialStatus | None = Query(default=None, alias="status"),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role(Role.admin, Role.editor)),
) -> Paginated[EditorialRead]:
    items, total = await editorial_service.list_managed_content(
        db,
        content_type=content_type.value if content_type else None,
        content_status=content_status.value if content_status else None,
        skip=skip,
        limit=limit,
    )
    return Paginated(
        items=[await _read(db, item) for item in items],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get("/id/{content_id}", response_model=EditorialRead)
async def get_editorial_for_editor(
    content_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role(Role.admin, Role.editor)),
) -> EditorialRead:
    content = await editorial_service.get_by_id(db, content_id)
    if content is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content not found")
    return await _read(db, content)


@router.get("/{content_type}/{slug}", response_model=EditorialRead)
async def get_editorial_by_slug(
    content_type: EditorialType, slug: str, db: AsyncSession = Depends(get_db)
) -> EditorialRead:
    content = await editorial_service.get_published_by_slug(db, content_type.value, slug)
    if content is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content not found")
    return await _read(db, content)


@router.post("", response_model=EditorialRead, status_code=status.HTTP_201_CREATED)
async def create_editorial(
    payload: EditorialCreate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role(Role.admin, Role.editor)),
) -> EditorialRead:
    try:
        content = await editorial_service.create_content(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return await _read(db, content)


@router.patch("/id/{content_id}", response_model=EditorialRead)
async def update_editorial(
    content_id: uuid.UUID,
    payload: EditorialUpdate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role(Role.admin, Role.editor)),
) -> EditorialRead:
    content = await editorial_service.get_by_id(db, content_id)
    if content is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content not found")
    try:
        content = await editorial_service.update_content(db, content, payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return await _read(db, content)


@router.delete("/id/{content_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_editorial(
    content_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role(Role.admin)),
) -> None:
    content = await editorial_service.get_by_id(db, content_id)
    if content is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content not found")
    await editorial_service.delete_content(db, content)
