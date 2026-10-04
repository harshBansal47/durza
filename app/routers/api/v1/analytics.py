"""Analytics event ingestion.

Public, rate-limited endpoint for recording coupon impressions and other
analytics events. Fire-and-forget: always returns 202 Accepted, never
blocks the page load.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.limiter import limiter
from app.models.coupon_impression import CouponImpression, ImpressionType

router = APIRouter(prefix="/analytics", tags=["analytics"])


class ImpressionRequest(BaseModel):
    coupon_id: uuid.UUID
    merchant_id: uuid.UUID
    page_type: ImpressionType
    page_id: str | None = Field(default=None, max_length=255)
    position: int = Field(ge=1, le=100)
    ranking_version: str = Field(default="v1", max_length=20)
    ranking_score: float | None = None
    source: str | None = Field(default=None, max_length=100)
    referrer: str | None = Field(default=None, max_length=500)
    device_type: str | None = Field(default=None, max_length=20)
    country: str | None = Field(default=None, max_length=2)
    sponsored: bool = False


@router.post("/impression", status_code=status.HTTP_202_ACCEPTED)
@limiter.limit("60/minute")
async def record_impression(
    payload: ImpressionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Record a coupon impression. Idempotent by design: no dedup needed
    because each render is a unique event from the user's perspective."""
    impression = CouponImpression(
        coupon_id=payload.coupon_id,
        merchant_id=payload.merchant_id,
        session_id=request.headers.get("x-session-id"),
        visitor_id=request.cookies.get("cz_vid"),
        page_type=payload.page_type,
        page_id=payload.page_id,
        position=payload.position,
        ranking_version=payload.ranking_version,
        ranking_score=payload.ranking_score,
        source=payload.source,
        referrer=payload.referrer,
        device_type=payload.device_type,
        country=payload.country,
        sponsored=payload.sponsored,
        created_at=datetime.now(UTC),
    )
    db.add(impression)
    await db.commit()
