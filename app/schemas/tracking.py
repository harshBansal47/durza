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
    telegram_enabled: bool
    telegram_chat_id: str | None
    push_enabled: bool
    push_subscription: str | None


class NotificationPreferenceUpdate(BaseModel):
    email_enabled: bool | None = None
    telegram_enabled: bool | None = None
    telegram_chat_id: str | None = None
    push_enabled: bool | None = None
    push_subscription: str | None = None


class PushKeys(BaseModel):
    model_config = ConfigDict(extra="ignore")

    p256dh: str = Field(min_length=1, max_length=255)
    auth: str = Field(min_length=1, max_length=255)


class PushSubscription(BaseModel):
    """A browser Push API subscription, as `PushManager.subscribe()` returns it.

    Validated rather than stored as free-form JSON, because pywebpush needs
    `endpoint` and both key halves to be present and well-formed. A subscription
    stored without them makes `push_enabled` look true while every send fails —
    the worst of both worlds, since the UI promises notifications nobody gets.
    """

    model_config = ConfigDict(extra="allow")

    endpoint: str = Field(min_length=1, max_length=2048)
    expiration_time: int | None = None
    keys: PushKeys


class PushSubscriptionRead(BaseModel):
    """What the settings page needs to render the current state.

    Deliberately does not echo `push_subscription` back. It is a bearer-ish
    credential for the push service, it is long and JSON-shaped, and the UI only
    ever needs to know whether one exists.
    """

    push_enabled: bool
    has_subscription: bool


class PushPublicKeyRead(BaseModel):
    """The VAPID public key the browser needs to call `subscribe()`.

    `enabled: false` is a first-class answer, not an error: it lets the settings
    page say "push is not available on this deployment" instead of the browser
    throwing an opaque error deep inside `subscribe()`.
    """

    public_key: str | None
    enabled: bool


class AlertEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: AlertKind
    channel: str
    detail: str | None
    delivered: bool
    price_point_id: uuid.UUID | None
    created_at: datetime
