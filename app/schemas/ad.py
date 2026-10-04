import uuid
from datetime import datetime
from urllib.parse import urlparse

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.ad import AD_LABELS, AdPosition


def _http_url(value: str | None) -> str | None:
    """Ads are rendered as links and images, so only real web URLs are allowed.
    This rejects `javascript:`, `data:` and similar before they reach a template."""
    if value is None:
        return None
    url = value.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("must be an http:// or https:// URL")
    return url


def _label(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in AD_LABELS:
        raise ValueError(f"label must be one of: {', '.join(AD_LABELS)}")
    return value


class _AdFields(BaseModel):
    @field_validator("image_url", "target_url", check_fields=False)
    @classmethod
    def _urls(cls, v: str | None) -> str | None:
        return _http_url(v)

    @field_validator("label", check_fields=False)
    @classmethod
    def _labels(cls, v: str | None) -> str | None:
        return _label(v)

    @model_validator(mode="after")
    def _window(self):
        starts = getattr(self, "starts_at", None)
        ends = getattr(self, "ends_at", None)
        if starts is not None and ends is not None and ends <= starts:
            raise ValueError("ends_at must be after starts_at")
        return self


class AdCreate(_AdFields):
    position: AdPosition
    image_url: str = Field(max_length=500)
    target_url: str = Field(max_length=1000)
    label: str = "Sponsored"
    advertiser: str | None = Field(default=None, max_length=120)
    alt_text: str | None = Field(default=None, max_length=200)
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None


class AdUpdate(_AdFields):
    position: AdPosition | None = None
    image_url: str | None = Field(default=None, max_length=500)
    target_url: str | None = Field(default=None, max_length=1000)
    label: str | None = None
    advertiser: str | None = Field(default=None, max_length=120)
    alt_text: str | None = Field(default=None, max_length=200)
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    is_active: bool | None = None


class AdRead(BaseModel):
    """Public shape. Deliberately has no `target_url`: the only way to reach the
    advertiser is GET /ads/{id}/go, so every click is recorded."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    position: AdPosition
    image_url: str
    label: str
    advertiser: str | None
    alt_text: str | None
    starts_at: datetime | None
    ends_at: datetime | None
    is_active: bool
    created_at: datetime


class AdAdminRead(AdRead):
    """Returned to editors/admins when they create or change an ad."""

    target_url: str
    clicks_count: int
