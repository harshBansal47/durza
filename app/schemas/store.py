import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.affiliate import AffiliateNetwork


def _validate_country(value: str | None) -> str | None:
    if value is None:
        return None
    code = value.strip().upper()
    if len(code) != 2 or not code.isalpha():
        raise ValueError("country_code must be a 2-letter ISO 3166-1 alpha-2 code")
    return code


def _validate_currency(value: str | None) -> str | None:
    if value is None:
        return None
    code = value.strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ValueError("currency must be a 3-letter ISO 4217 code")
    return code


def _validate_network(value: str | None) -> str | None:
    if value is None:
        return None
    network = value.strip().lower()
    if network not in {n.value for n in AffiliateNetwork}:
        allowed = ", ".join(n.value for n in AffiliateNetwork)
        raise ValueError(f"affiliate_network must be one of: {allowed}")
    return network


def _validate_template(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    template = value.strip()
    if not template.lower().startswith(("http://", "https://")):
        raise ValueError("link_template must start with http:// or https://")
    return template


class StoreFAQInput(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    answer: str = Field(min_length=1)
    sort_order: int = Field(default=0, ge=0)


class StoreCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    logo_url: str | None = None
    website_url: str | None = None
    description: str | None = None
    pricing_summary: dict[str, Any] | None = None
    pricing_checked_at: datetime | None = None
    commission_disclosure: str | None = None
    country_code: str | None = None
    currency: str | None = None
    affiliate_network: str | None = Field(default=None)
    link_template: str | None = Field(default=None, max_length=1000)
    cookie_days: int | None = Field(default=None, ge=0, le=365)
    faqs: list[StoreFAQInput] = Field(default_factory=list)
    alternative_store_ids: list[uuid.UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_alternatives(self) -> "StoreCreate":
        if len(self.alternative_store_ids) != len(set(self.alternative_store_ids)):
            raise ValueError("alternative_store_ids must be unique")
        return self

    _check_country = field_validator("country_code")(_validate_country)
    _check_currency = field_validator("currency")(_validate_currency)
    _check_network = field_validator("affiliate_network")(_validate_network)
    _check_template = field_validator("link_template")(_validate_template)


class StoreUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=150)
    logo_url: str | None = None
    website_url: str | None = None
    description: str | None = None
    pricing_summary: dict[str, Any] | None = None
    pricing_checked_at: datetime | None = None
    is_active: bool | None = None
    commission_disclosure: str | None = None
    country_code: str | None = None
    currency: str | None = None
    affiliate_network: str | None = None
    link_template: str | None = Field(default=None, max_length=1000)
    cookie_days: int | None = Field(default=None, ge=0, le=365)
    faqs: list[StoreFAQInput] | None = None
    alternative_store_ids: list[uuid.UUID] | None = None

    @model_validator(mode="after")
    def unique_alternatives(self) -> "StoreUpdate":
        if self.alternative_store_ids is not None and len(self.alternative_store_ids) != len(
            set(self.alternative_store_ids)
        ):
            raise ValueError("alternative_store_ids must be unique")
        return self

    _check_country = field_validator("country_code")(_validate_country)
    _check_currency = field_validator("currency")(_validate_currency)
    _check_network = field_validator("affiliate_network")(_validate_network)
    _check_template = field_validator("link_template")(_validate_template)


class StoreRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    logo_url: str | None
    website_url: str | None
    description: str | None
    pricing_summary: dict[str, Any] | None
    pricing_checked_at: datetime | None
    is_active: bool
    commission_disclosure: str | None
    country_code: str | None
    currency: str | None
    created_at: datetime
