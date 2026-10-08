import uuid

from pydantic import BaseModel, ConfigDict

from app.schemas.coupon_public import CouponPublicRead
from app.schemas.editorial import EditorialRead
from app.schemas.store import StoreRead


class StoreFAQRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    question: str
    answer: str
    sort_order: int


class StoreDestinationRead(BaseModel):
    store: StoreRead
    deals: list[CouponPublicRead]
    coupons: list[CouponPublicRead]
    faqs: list[StoreFAQRead]
    alternatives: list[StoreRead]
    related_content: list[EditorialRead]
