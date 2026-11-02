import uuid

from pydantic import BaseModel, ConfigDict

class SavedItemRead(BaseModel):
    """A saved coupon or store returned to the account UI."""

    kind: str  # "store" | "coupon"
    item_id: uuid.UUID
    saved_id: uuid.UUID


class NotificationPreferenceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    email_enabled: bool


class NotificationPreferenceUpdate(BaseModel):
    email_enabled: bool | None = None
