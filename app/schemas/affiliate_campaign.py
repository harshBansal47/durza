"""Public campaign data and private synchronization summaries."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.core.pagination import Paginated


class AffiliateCampaignRead(BaseModel):
    id: uuid.UUID
    title: str
    description: str | None
    kpi: str | None
    merchant_name: str
    store_id: uuid.UUID | None
    preview_url: str | None
    thumbnail_url: str | None
    categories: list[str]
    countries: list[str]
    conversion_model: str | None
    currency: str | None
    affiliate_payouts: Any | None
    default_goal: Any | None
    goals: Any | None
    device: Any | None
    operating_system: Any | None
    app_name: str | None
    app_id: str | None
    is_active: bool
    last_synced_at: datetime
    click_url: str


class AffiliateCampaignPage(Paginated[AffiliateCampaignRead]):
    pass


class AffiliateCampaignSyncRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    started_at: datetime
    finished_at: datetime | None
    pages_fetched: int
    inserted: int
    updated: int
    unchanged: int
    skipped: int
    deactivated: int
    error: str | None


class AffiliateCampaignSyncResponse(BaseModel):
    detail: str
    latest_sync: AffiliateCampaignSyncRead | None = None
