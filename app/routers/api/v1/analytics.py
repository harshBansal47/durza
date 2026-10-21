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

from app.core.config import Settings, get_settings
from app.core.consent import has_valid_consent, issue_consent_cookie
from app.core.database import get_db
from app.core.deps import require_role
from app.core.limiter import limiter
from app.models.analytics_event import AnalyticsEvent
from app.models.coupon_impression import CouponImpression, ImpressionType
from app.models.daily_stats import DailyContentStats
from app.models.user import Role, User
from app.services.visitor_identity import get_or_create_session

router = APIRouter(prefix="/analytics", tags=["analytics"])


class ImpressionRequest(BaseModel):
    coupon_id: uuid.UUID
    merchant_id: uuid.UUID
    page_type: ImpressionType
    page_id: str | None = Field(default=None, max_length=255)
    list_id: str | None = Field(default=None, max_length=80)
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
    settings = get_settings()
    if not _has_consent(request, settings):
        return

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
        list_id=payload.list_id,
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
    content_view = "content_view"
    scroll = "scroll"
    engaged_time = "engaged_time"
    outbound_click = "outbound_click"
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
_NOT_PROPS = {
    "event",
    "coupon_id",
    "merchant_id",
    "store_id",
    "page_type",
    "content_id",
    "content_type",
    "list_id",
    "position",
    "ranking_version",
    "schema_version",
    "ts",
}


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
    model_config = ConfigDict(extra="allow")

    event: EventName
    coupon_id: str | None = Field(default=None, max_length=64)
    merchant_id: str | None = Field(default=None, max_length=64)
    store_id: str | None = Field(default=None, max_length=64)
    page_type: str | None = Field(default=None, max_length=30)
    content_id: str | None = Field(default=None, max_length=80)
    content_type: str | None = Field(default=None, max_length=30)
    list_id: str | None = Field(default=None, max_length=80)
    position: int | None = Field(default=None, ge=1, le=100)
    ranking_version: str | None = Field(default=None, max_length=20)
    schema_version: int | None = Field(default=None, ge=1)


@router.post("/event", status_code=status.HTTP_202_ACCEPTED)
@limiter.limit("120/minute")
async def record_event(
    payload: EventRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Record a shopper interaction. Fire-and-forget like impressions: 202, no body.

    Nothing is stored without a verifiable consent cookie. The response is
    still 202, so a visitor who never agreed sees no difference in the app —
    the event is simply not kept.
    """
    settings = get_settings()
    if not _has_consent(request, settings):
        return
    db.add(
        AnalyticsEvent(
            name=payload.event.value,
            session_id=request.headers.get("x-session-id")
            or request.cookies.get("cz_sid")
            or str((await get_or_create_session(request, response, db)).id),
            visitor_id=request.cookies.get("cz_vid"),
            coupon_id=_uuid_or_none(payload.coupon_id),
            store_id=_uuid_or_none(payload.merchant_id or payload.store_id),
            page_type=payload.page_type,
            content_id=payload.content_id,
            content_type=payload.content_type,
            list_id=payload.list_id,
            position=payload.position,
            ranking_version=payload.ranking_version,
            schema_version=payload.schema_version or 1,
            props=sanitize_props(payload.model_extra),
            created_at=datetime.now(UTC),
        )
    )
    await db.commit()


class ConsentChoice(str, enum.Enum):
    accepted = "accepted"
    declined = "declined"


class ConsentRequest(BaseModel):
    choice: ConsentChoice


def _has_consent(request: Request, settings: Settings) -> bool:
    """Whether the request carries a verifiable consent cookie."""
    return has_valid_consent(
        request.cookies.get(settings.analytics_consent_cookie),
        secret=settings.secret_key,
        max_age_days=settings.analytics_consent_max_age_days,
    )


@router.post("/consent", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def record_consent(
    payload: ConsentRequest,
    request: Request,
    response: Response,
) -> None:
    """Record a consent choice.

    On accept, the server signs the choice into an HttpOnly cookie scoped to
    the parent domain, so the API can tell that the visitor agreed — the
    client-side flag is not evidence of anything. On decline, the cookie and
    the visitor/session cookies are cleared.
    """
    settings = get_settings()
    if payload.choice == ConsentChoice.declined:
        response.delete_cookie(settings.analytics_consent_cookie, path="/", samesite="lax")
        response.delete_cookie("cz_vid", path="/", samesite="lax")
        response.delete_cookie("cz_sid", path="/", samesite="lax")
        return

    cookie = issue_consent_cookie(
        secret=settings.secret_key,
        max_age_days=settings.analytics_consent_max_age_days,
        site_url=settings.site_url,
    )
    response.set_cookie(
        settings.analytics_consent_cookie,
        cookie.value,
        max_age=cookie.max_age,
        path="/",
        domain=cookie.domain,
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
    )


from datetime import date as _date

from fastapi import Query as _Query


@router.get("/content", status_code=status.HTTP_200_OK)
@limiter.limit("30/minute")
async def content_analytics(
    request: Request,
    start_date: _date = _Query(...),
    end_date: _date = _Query(...),
    content_type: str | None = _Query(default=None, max_length=30),
    page: int = _Query(default=1, ge=1),
    page_size: int = _Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role(Role.admin, Role.analytics_viewer)),
) -> dict:
    from sqlalchemy import func, select

    stmt = select(DailyContentStats).where(
        DailyContentStats.date >= start_date,
        DailyContentStats.date <= end_date,
    )
    if content_type:
        stmt = stmt.where(DailyContentStats.content_type == content_type)
    stmt = stmt.order_by(DailyContentStats.date.desc(), DailyContentStats.content_ref.asc())
    offset = (page - 1) * page_size
    rows = (await db.execute(stmt.offset(offset).limit(page_size))).scalars().all()
    total = (
        await db.execute(
            select(func.count())
            .select_from(DailyContentStats)
            .where(
                DailyContentStats.date >= start_date,
                DailyContentStats.date <= end_date,
                *([DailyContentStats.content_type == content_type] if content_type else []),
            )
        )
    ).scalar_one()
    return {
        "items": [
            {
                "date": r.date.isoformat(),
                "content_ref": r.content_ref,
                "content_type": r.content_type,
                "views": r.views,
                "outbound_clicks": r.outbound_clicks,
                "confirmed_revenue": r.confirmed_revenue,
                "currency": r.currency,
            }
            for r in rows
        ],
        "page": page,
        "page_size": page_size,
        "total": total,
    }
