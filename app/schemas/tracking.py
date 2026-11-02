import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.tracking import AlertKind


class SavedItemRead(BaseModel):
    kind: str  # "store" | "coupon"
    item_id: uuid.UUID
    saved_id: uuid.UUID


class TrackedProductCreate(BaseModel):
    product_id: uuid.UUID
    target_price: float | None = Field(default=None, gt=0)


class TrackedProductUpdate(BaseModel):
    target_price: float | None = Field(default=None, gt=0)


class TrackedProductRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    product_id: uuid.UUID
    target_price: float | None
    created_at: datetime


class NotificationPreferenceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    email_enabled: bool
    muted_categories: list[str] = Field(default_factory=list)


class NotificationPreferenceUpdate(BaseModel):
    email_enabled: bool | None = None
    muted_categories: list[str] | None = None


class AlertEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: AlertKind
    channel: str
    detail: str | None
    delivered: bool
    price_point_id: uuid.UUID | None
    # Deal alerts: the offer / store the alert leads with, and one id shared by
    # every channel row of the same alert (the history groups on it).
    coupon_id: uuid.UUID | None = None
    store_id: uuid.UUID | None = None
    batch_id: uuid.UUID | None = None
    created_at: datetime
