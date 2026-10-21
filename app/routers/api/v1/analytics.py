"""Analytics event ingestion.

Public, rate-limited endpoint for recording coupon impressions and other
analytics events. Fire-and-forget: always returns 202 Accepted, never
blocks the page load.
"""

import enum
import re
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.limiter import limiter
from app.models.analytics_event import AnalyticsEvent
from app.models.coupon_impression import CouponImpression, ImpressionType
from app.services.visitor_identity import get_or_create_session

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
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Record a coupon impression. Idempotent by design: no dedup needed
    because each render is a unique event from the user's perspective."""
    session_id = request.headers.get("x-session-id") or request.cookies.get("cz_sid")
    if not session_id:
        session = await get_or_create_session(request, response, db)
        session_id = str(session.id)

    country = payload.country or request.headers.get("x-vercel-ip-country")

    impression = CouponImpression(
        coupon_id=payload.coupon_id,
        merchant_id=payload.merchant_id,
        session_id=session_id,
        visitor_id=request.cookies.get("cz_vid"),
        page_type=payload.page_type,
        page_id=payload.page_id,
        position=payload.position,
        ranking_version=payload.ranking_version,
        ranking_score=payload.ranking_score,
        source=payload.source,
        referrer=payload.referrer,
        device_type=payload.device_type,
        country=country,
        sponsored=payload.sponsored,
        created_at=datetime.now(UTC),
    )
    db.add(impression)
    await db.commit()


class EventName(str, enum.Enum):
    """Shopper interactions the storefront may report. Impressions are separate
    (POST /analytics/impression). An unknown name is rejected, so the table can only
    ever hold events the product actually defines."""

    page_view = "page_view"
    search = "search"
    coupon_click = "coupon_click"
    coupon_reveal = "coupon_reveal"
    copy_code = "copy_code"
    save_coupon = "save_coupon"
    unsave_coupon = "unsave_coupon"
    follow_store = "follow_store"
    unfollow_store = "unfollow_store"
    merchant_click = "merchant_click"
    filter_change = "filter_change"
    verify_vote = "verify_vote"


_PROP_KEY = re.compile(r"^[a-z][a-z0-9_]{0,29}$")
_MAX_PROPS = 10
_MAX_STR = 200
# Reserved: carried in their own columns, or sent by the client but not worth storing.
_NOT_PROPS = {"event", "coupon_id", "merchant_id", "store_id", "page_type", "ts"}


def sanitize_props(extra: dict | None) -> dict | None:
    """Keep a handful of short scalar values. Nested objects, long strings and oddly
    named keys are dropped rather than stored, so a client cannot use this endpoint as
    free-form storage."""
    if not extra:
        return None
    clean: dict[str, str | int | float | bool] = {}
    for key, value in extra.items():
        if key in _NOT_PROPS or not _PROP_KEY.match(key):
            continue
        if isinstance(value, bool | int | float):
            clean[key] = value
        elif isinstance(value, str):
            clean[key] = value[:_MAX_STR]
        if len(clean) >= _MAX_PROPS:
            break
    return clean or None


def _uuid_or_none(value: object) -> uuid.UUID | None:
    """A malformed id must cost one field, not the whole event."""
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


class EventRequest(BaseModel):
    # The storefront sends a flat object (`{event, coupon_id, ..., query}`); anything
    # beyond the named fields is collected as properties.
    model_config = ConfigDict(extra="allow")

    event: EventName
    coupon_id: str | None = Field(default=None, max_length=64)
    merchant_id: str | None = Field(default=None, max_length=64)
    store_id: str | None = Field(default=None, max_length=64)
    page_type: str | None = Field(default=None, max_length=30)


@router.post("/event", status_code=status.HTTP_202_ACCEPTED)
@limiter.limit("120/minute")
async def record_event(
    payload: EventRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Record a shopper interaction. Fire-and-forget like impressions: 202, no body."""
    db.add(
        AnalyticsEvent(
            name=payload.event.value,
            session_id=request.headers.get("x-session-id") or request.cookies.get("cz_sid") or str((await get_or_create_session(request, response, db)).id),
            visitor_id=request.cookies.get("cz_vid"),
            coupon_id=_uuid_or_none(payload.coupon_id),
            store_id=_uuid_or_none(payload.merchant_id or payload.store_id),
            page_type=payload.page_type,
            props=sanitize_props(payload.model_extra),
            created_at=datetime.now(UTC),
        )
    )
    await db.commit()
