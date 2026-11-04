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
    # Every live offer, not just the (up to 100 + 100) listed above. Lets the store page show
    # an exact count without a second listing request.
    offer_total: int = 0
