import uuid
from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.models.coupon import DiscountType


class CouponCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    code: str | None = Field(default=None, max_length=50)
    description: str | None = None
    discount_type: DiscountType
    discount_value: float | None = Field(default=None, ge=0)
    store_id: uuid.UUID
    category_id: uuid.UUID
    destination_url: str = Field(min_length=1, max_length=1000)
    expires_at: AwareDatetime | None = None


class CouponUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    code: str | None = Field(default=None, max_length=50)
    description: str | None = None
    discount_type: DiscountType | None = None
    discount_value: float | None = Field(default=None, ge=0)
    store_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    destination_url: str | None = Field(default=None, min_length=1, max_length=1000)
    expires_at: AwareDatetime | None = None
    is_active: bool | None = None


class CouponRead(BaseModel):
    """Full view, including destination_url. Phase 5 adds a public-safe view + /go redirect."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    slug: str
    code: str | None
    description: str | None
    discount_type: DiscountType
    discount_value: float | None
    store_id: uuid.UUID
    category_id: uuid.UUID
    destination_url: str
    expires_at: datetime | None
    is_active: bool
    views_count: int
    clicks_count: int
    created_at: datetime
