import uuid
from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ProductCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    store_id: uuid.UUID
    category_id: uuid.UUID
    url: str | None = Field(default=None, max_length=1000)
    image_url: str | None = Field(default=None, max_length=1000)
    currency: str = Field(default="INR", min_length=3, max_length=3)


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    store_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    url: str | None = Field(default=None, max_length=1000)
    image_url: str | None = Field(default=None, max_length=1000)
    currency: str | None = Field(default=None, min_length=3, max_length=3)


class ProductRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    store_id: uuid.UUID
    category_id: uuid.UUID
    # The raw store URL is deliberately absent: it is reachable only through
    # GET /products/{id}/go so every outbound click is tracked.
    has_url: bool
    image_url: str | None
    currency: str
    current_price: float | None
    list_price: float | None
    in_stock: bool
    last_captured_at: datetime | None
    lowest_price_7d: float | None
    lowest_price_30d: float | None
    lowest_price_90d: float | None
    last_price_drop_at: datetime | None
    last_price_drop_pct: float | None
    created_at: datetime
    # Filled in by the router from the latest PricePoint's coupon.
    effective_price: float | None = None


class PricePointCreate(BaseModel):
    price: float = Field(gt=0)
    original_price: float | None = Field(default=None, ge=0)
    shipping: float = Field(default=0, ge=0)
    in_stock: bool = True
    coupon_id: uuid.UUID | None = None
    captured_at: AwareDatetime | None = None


class PricePointRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    product_id: uuid.UUID
    price: float
    original_price: float | None
    shipping: float
    in_stock: bool
    coupon_id: uuid.UUID | None
    captured_at: datetime
