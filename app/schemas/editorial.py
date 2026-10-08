import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


class EditorialType(str, Enum):
    guide = "guide"
    review = "review"
    comparison = "comparison"
    collection = "collection"


class EditorialStatus(str, Enum):
    draft = "draft"
    in_review = "in_review"
    published = "published"
    archived = "archived"


class EditorialRelationWrite(BaseModel):
    related_content_id: uuid.UUID
    relation_type: Literal["related", "supports", "alternative", "collection_member"] = "related"
    sort_order: int = Field(default=0, ge=0)


class EditorialEntityLinkWrite(BaseModel):
    store_id: uuid.UUID | None = None
    product_id: uuid.UUID | None = None
    coupon_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    link_type: Literal["primary", "related", "mentioned", "featured"] = "related"
    anchor_text: str | None = Field(default=None, max_length=180)
    sort_order: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def has_exactly_one_entity(self) -> "EditorialEntityLinkWrite":
        if sum(value is not None for value in (
            self.store_id, self.product_id, self.coupon_id, self.category_id
        )) != 1:
            raise ValueError("exactly one of store_id, product_id, coupon_id, or category_id is required")
        return self


class FAQWrite(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    answer: str = Field(min_length=1)
    sort_order: int = Field(default=0, ge=0)


class EditorialCreate(BaseModel):
    content_type: EditorialType
    title: str = Field(min_length=1, max_length=240)
    slug: str | None = Field(default=None, min_length=1, max_length=260)
    summary: str = ""
    content_blocks: list[dict[str, Any]] = Field(default_factory=list)
    structured_data: dict[str, Any] = Field(default_factory=dict)
    hero_image_url: str | None = Field(default=None, max_length=1000)
    author_name: str | None = Field(default=None, max_length=160)
    market: str | None = Field(default=None, min_length=2, max_length=2)
    seo_title: str | None = Field(default=None, max_length=240)
    meta_description: str | None = Field(default=None, max_length=300)
    canonical_url: str | None = Field(default=None, max_length=1000)
    is_indexable: bool = True
    is_featured: bool = False
    status: EditorialStatus = EditorialStatus.draft
    relations: list[EditorialRelationWrite] = Field(default_factory=list)
    entity_links: list[EditorialEntityLinkWrite] = Field(default_factory=list)
    faqs: list[FAQWrite] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_relations(self) -> "EditorialCreate":
        keys = [
            (item.related_content_id, item.relation_type)
            for item in self.relations
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("Editorial relations must be unique")
        return self


class EditorialUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    slug: str | None = Field(default=None, min_length=1, max_length=260)
    summary: str | None = None
    content_blocks: list[dict[str, Any]] | None = None
    structured_data: dict[str, Any] | None = None
    hero_image_url: str | None = Field(default=None, max_length=1000)
    author_name: str | None = Field(default=None, max_length=160)
    market: str | None = Field(default=None, min_length=2, max_length=2)
    seo_title: str | None = Field(default=None, max_length=240)
    meta_description: str | None = Field(default=None, max_length=300)
    canonical_url: str | None = Field(default=None, max_length=1000)
    is_indexable: bool | None = None
    is_featured: bool | None = None
    status: EditorialStatus | None = None
    relations: list[EditorialRelationWrite] | None = None
    entity_links: list[EditorialEntityLinkWrite] | None = None
    faqs: list[FAQWrite] | None = None

    @model_validator(mode="after")
    def reject_null_for_required_fields(self) -> "EditorialUpdate":
        required = {
            "title",
            "slug",
            "summary",
            "content_blocks",
            "structured_data",
            "is_indexable",
            "is_featured",
            "status",
        }
        for field in required & self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        if self.relations is not None:
            keys = [
                (item.related_content_id, item.relation_type)
                for item in self.relations
            ]
            if len(keys) != len(set(keys)):
                raise ValueError("Editorial relations must be unique")
        return self


class EditorialRelationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    related_content_id: uuid.UUID
    relation_type: str
    sort_order: int


class EditorialEntityLinkRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    store_id: uuid.UUID | None
    product_id: uuid.UUID | None
    coupon_id: uuid.UUID | None
    category_id: uuid.UUID | None
    link_type: str
    anchor_text: str | None
    sort_order: int


class FAQRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    question: str
    answer: str
    sort_order: int


class EditorialRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    content_type: EditorialType
    status: EditorialStatus
    title: str
    slug: str
    summary: str
    content_blocks: list[dict[str, Any]]
    structured_data: dict[str, Any]
    hero_image_url: str | None
    author_name: str | None
    market: str | None
    seo_title: str | None
    meta_description: str | None
    canonical_url: str | None
    is_indexable: bool
    is_featured: bool
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime
    relations: list[EditorialRelationRead] = Field(default_factory=list)
    entity_links: list[EditorialEntityLinkRead] = Field(default_factory=list)
    faqs: list[FAQRead] = Field(default_factory=list)

    @computed_field
    @property
    def adsense_eligible(self) -> bool:
        return (
            self.status is EditorialStatus.published
            and self.is_indexable
            and self.content_type in {
                EditorialType.guide,
                EditorialType.review,
                EditorialType.comparison,
            }
        )


class EditorialHomeRead(BaseModel):
    guides: list[EditorialRead]
    comparisons: list[EditorialRead]
    reviews: list[EditorialRead]
    collections: list[EditorialRead]
