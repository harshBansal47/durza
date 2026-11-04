"""Public vCommission campaign catalogue and admin synchronization controls."""

import uuid
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_role
from app.core.limiter import limiter
from app.core.pagination import Paginated
from app.core.request_meta import client_ip, hash_ip
from app.models.affiliate_campaign import AffiliateCampaign, AffiliateCampaignSync
from app.models.user import Role, User
from app.schemas.affiliate_campaign import (
    AffiliateCampaignRead,
    AffiliateCampaignSyncRead,
    AffiliateCampaignSyncResponse,
)
from app.services import click_service
from app.services.scheduler import run_job_now
from app.services.vcommission_service import VCommissionSyncError, get_campaign, list_campaigns

router = APIRouter(prefix="/vcommission", tags=["vcommission"])


def _campaign_read(campaign: AffiliateCampaign) -> AffiliateCampaignRead:
    return AffiliateCampaignRead(
        id=campaign.id,
        title=campaign.title,
        description=campaign.description,
        kpi=campaign.kpi,
        merchant_name=campaign.merchant_name,
        store_id=campaign.store_id,
        preview_url=campaign.preview_url,
        thumbnail_url=campaign.thumbnail_url,
        categories=campaign.categories,
        countries=campaign.countries,
        conversion_model=campaign.conversion_model,
        currency=campaign.currency,
        affiliate_payouts=campaign.payouts,
        default_goal=campaign.default_goal,
        goals=campaign.goals,
        device=campaign.device,
        operating_system=campaign.operating_system,
        app_name=campaign.app_name,
        app_id=campaign.app_id,
        is_active=campaign.is_active,
        last_synced_at=campaign.last_synced_at,
        click_url=f"/api/v1/vcommission/campaigns/{campaign.id}/go",
    )


@router.get("/campaigns", response_model=Paginated[AffiliateCampaignRead])
async def get_campaigns(
    search: str | None = Query(default=None, max_length=100),
    category: str | None = Query(default=None, max_length=120),
    country: str | None = Query(default=None, min_length=2, max_length=2),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> Paginated[AffiliateCampaignRead]:
    campaigns, total = await list_campaigns(
        db, search=search, category=category, country=country, skip=skip, limit=limit
    )
    return Paginated(
        items=[_campaign_read(campaign) for campaign in campaigns],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get("/campaigns/{campaign_id}/go", response_class=RedirectResponse)
@limiter.limit("30/minute")
async def go_to_campaign(
    campaign_id: uuid.UUID,
    request: Request,
    src: str | None = Query(default=None, max_length=40),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Track a click and redirect only to the destination stored by a sync."""
    campaign = await get_campaign(db, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    try:
        target = urlsplit(campaign.tracking_url)
        if (
            target.scheme not in {"http", "https"}
            or not target.hostname
            or target.username is not None
            or target.password is not None
        ):
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=404, detail="Campaign not found") from None

    store = await click_service.get_store(db, campaign.store_id) if campaign.store_id else None
    await click_service.record_click(
        db,
        store=store,
        network="vcommission",
        src=src,
        visitor_hash=hash_ip(client_ip(request)),
        user_agent=request.headers.get("user-agent"),
    )
    # vCommission's clickref parameter is not documented in this integration.
    # Preserve the network-provided tracking URL exactly rather than guessing.
    return RedirectResponse(campaign.tracking_url, status_code=status.HTTP_302_FOUND)


@router.get("/campaigns/{campaign_id}", response_model=AffiliateCampaignRead)
async def get_campaign_detail(
    campaign_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> AffiliateCampaignRead:
    campaign = await get_campaign(db, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return _campaign_read(campaign)


@router.post("/admin/sync", response_model=AffiliateCampaignSyncResponse)
@limiter.limit("3/hour")
async def sync_vcommission_campaigns(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role(Role.admin)),
) -> AffiliateCampaignSyncResponse:
    """Synchronize on demand under the same advisory lock as the scheduled job."""
    try:
        detail = await run_job_now("sync_vcommission_campaigns")
    except VCommissionSyncError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    latest = await db.scalar(
        select(AffiliateCampaignSync).order_by(AffiliateCampaignSync.started_at.desc()).limit(1)
    )
    return AffiliateCampaignSyncResponse(
        detail=detail,
        latest_sync=AffiliateCampaignSyncRead.model_validate(latest) if latest else None,
    )


@router.get("/admin/sync/latest", response_model=AffiliateCampaignSyncRead)
async def get_latest_vcommission_sync(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role(Role.admin)),
) -> AffiliateCampaignSync:
    latest = await db.scalar(
        select(AffiliateCampaignSync).order_by(AffiliateCampaignSync.started_at.desc()).limit(1)
    )
    if latest is None:
        raise HTTPException(status_code=404, detail="No campaign sync has run yet")
    return latest
