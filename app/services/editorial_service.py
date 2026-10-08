import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import paginate
from app.core.slugs import generate_unique_slug
from app.models.editorial import (
    EditorialContent,
    EditorialEntityLink,
    EditorialFAQ,
    EditorialRelation,
)
from app.schemas.editorial import EditorialCreate, EditorialUpdate

def _validate_publish(content: EditorialContent, status: str) -> None:
    if status == "published" and (not content.summary.strip() or not content.content_blocks):
        raise ValueError("Published content requires a summary and at least one content block")


async def list_content(
    db: AsyncSession,
    *,
    content_type: str | None = None,
    featured_only: bool = False,
    skip: int = 0,
    limit: int = 20,
) -> tuple[Sequence[EditorialContent], int]:
    stmt = select(EditorialContent).where(
        EditorialContent.status == "published",
        EditorialContent.is_indexable.is_(True),
    )
    if content_type is not None:
        stmt = stmt.where(EditorialContent.content_type == content_type)
    if featured_only:
        stmt = stmt.where(EditorialContent.is_featured.is_(True))
    return await paginate(
        db, stmt.order_by(EditorialContent.published_at.desc(), EditorialContent.title), skip, limit
    )


async def list_managed_content(
    db: AsyncSession,
    *,
    content_type: str | None = None,
    content_status: str | None = None,
    skip: int = 0,
    limit: int = 20,
) -> tuple[Sequence[EditorialContent], int]:
    stmt = select(EditorialContent)
    if content_type is not None:
        stmt = stmt.where(EditorialContent.content_type == content_type)
    if content_status is not None:
        stmt = stmt.where(EditorialContent.status == content_status)
    return await paginate(
        db, stmt.order_by(EditorialContent.updated_at.desc(), EditorialContent.title), skip, limit
    )


async def get_published_by_slug(
    db: AsyncSession, content_type: str, slug: str
) -> EditorialContent | None:
    result = await db.execute(
        select(EditorialContent).where(
            EditorialContent.content_type == content_type,
            EditorialContent.slug == slug,
            EditorialContent.status == "published",
        )
    )
    return result.scalar_one_or_none()


async def get_by_id(db: AsyncSession, content_id: uuid.UUID) -> EditorialContent | None:
    return await db.get(EditorialContent, content_id)


async def related_data(
    db: AsyncSession, content_id: uuid.UUID
) -> tuple[list[EditorialRelation], list[EditorialEntityLink], list[EditorialFAQ]]:
    relations = await db.scalars(
        select(EditorialRelation)
        .where(EditorialRelation.content_id == content_id)
        .order_by(EditorialRelation.sort_order, EditorialRelation.id)
    )
    entity_links = await db.scalars(
        select(EditorialEntityLink)
        .where(EditorialEntityLink.content_id == content_id)
        .order_by(EditorialEntityLink.sort_order, EditorialEntityLink.id)
    )
    faqs = await db.scalars(
        select(EditorialFAQ)
        .where(EditorialFAQ.content_id == content_id)
        .order_by(EditorialFAQ.sort_order, EditorialFAQ.id)
    )
    return list(relations), list(entity_links), list(faqs)


async def _replace_related_data(
    db: AsyncSession,
    content_id: uuid.UUID,
    *,
    relations: list | None,
    entity_links: list | None,
    faqs: list | None,
) -> None:
    if relations is not None:
        if any(item.related_content_id == content_id for item in relations):
            raise ValueError("Editorial content cannot relate to itself")
        await db.execute(delete(EditorialRelation).where(EditorialRelation.content_id == content_id))
        db.add_all(
            EditorialRelation(content_id=content_id, **item.model_dump()) for item in relations
        )
    if entity_links is not None:
        await db.execute(
            delete(EditorialEntityLink).where(EditorialEntityLink.content_id == content_id)
        )
        db.add_all(
            EditorialEntityLink(content_id=content_id, **item.model_dump())
            for item in entity_links
        )
    if faqs is not None:
        await db.execute(delete(EditorialFAQ).where(EditorialFAQ.content_id == content_id))
        db.add_all(EditorialFAQ(content_id=content_id, **item.model_dump()) for item in faqs)


async def create_content(db: AsyncSession, payload: EditorialCreate) -> EditorialContent:
    data = payload.model_dump(exclude={"relations", "entity_links", "faqs"})
    data["content_type"] = payload.content_type.value
    data["status"] = payload.status.value
    data["slug"] = await generate_unique_slug(db, EditorialContent, payload.slug or payload.title)
    if data["market"] is not None:
        data["market"] = data["market"].upper()
    content = EditorialContent(**data)
    _validate_publish(content, content.status)
    if content.status == "published":
        content.published_at = datetime.now(UTC)
    db.add(content)
    await db.flush()
    await _replace_related_data(
        db,
        content.id,
        relations=payload.relations,
        entity_links=payload.entity_links,
        faqs=payload.faqs,
    )
    await db.commit()
    await db.refresh(content)
    return content


async def update_content(
    db: AsyncSession, content: EditorialContent, payload: EditorialUpdate
) -> EditorialContent:
    data = payload.model_dump(
        exclude_unset=True, exclude={"relations", "entity_links", "faqs"}
    )
    if "status" in data and data["status"] is not None:
        data["status"] = data["status"].value
    if "slug" in data and data["slug"] is not None:
        data["slug"] = await generate_unique_slug(
            db, EditorialContent, data["slug"], exclude_id=content.id
        )
    if "market" in data and data["market"] is not None:
        data["market"] = data["market"].upper()
    was_published = content.status == "published"
    for field, value in data.items():
        setattr(content, field, value)
    _validate_publish(content, content.status)
    if content.status == "published" and not was_published:
        content.published_at = datetime.now(UTC)
    await _replace_related_data(
        db,
        content.id,
        relations=payload.relations,
        entity_links=payload.entity_links,
        faqs=payload.faqs,
    )
    await db.commit()
    await db.refresh(content)
    return content


async def delete_content(db: AsyncSession, content: EditorialContent) -> None:
    await db.delete(content)
    await db.commit()
