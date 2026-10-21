from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.limiter import limiter
from app.core.deps import get_current_user
from app.models.user import User
from app.models.user_interest import UserInterest
from app.services.segmentation import compute_segment
from sqlalchemy import select, delete

router = APIRouter(prefix="/personalization", tags=["personalization"])


def _no_store_headers(response):
    response.headers["Cache-Control"] = "private, no-store"


class Signal(BaseModel):
    interest_type: str = Field(pattern="^(category|store|bank)$")
    interest_key: str = Field(min_length=1, max_length=120)
    occurred_at: datetime


class SyncRequest(BaseModel):
    consent: bool
    signals: list[Signal] = Field(default_factory=list, max_length=get_settings().personalization_max_signals)


class ComputeRequest(BaseModel):
    consent: bool
    signals: list[Signal] = Field(default_factory=list, max_length=get_settings().personalization_max_signals)


def _decay(occurred_at: datetime, now: datetime, half_life_days: int) -> float:
    age = max(0.0, (now - occurred_at).total_seconds() / 86400.0)
    return 0.5 ** (age / max(1, half_life_days))


def _allowed(key: str, interest_type: str) -> bool:
    if interest_type != "category":
        return True
    allowed = [x.strip() for x in get_settings().personalization_allowed_categories.split(",") if x.strip()]
    return key in allowed


@router.post("/interests", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def sync_interests(
    payload: SyncRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not get_settings().personalization_enabled:
        raise HTTPException(status_code=404, detail="Not found", headers={"Cache-Control": "private, no-store"})
    if not payload.consent or not user.personalization_consent:
        raise HTTPException(status_code=403, detail="Personalization consent required", headers={"Cache-Control": "private, no-store"})
    _no_store_headers(response)
    now = datetime.now(UTC)
    ttl = timedelta(days=get_settings().personalization_ttl_days)
    for sig in payload.signals:
        if not _allowed(sig.interest_key, sig.interest_type):
            continue
        score = _decay(sig.occurred_at, now, get_settings().personalization_half_life_days)
        existing = (await db.execute(
            select(UserInterest).where(
                UserInterest.user_id == user.id,
                UserInterest.interest_type == sig.interest_type,
                UserInterest.interest_key == sig.interest_key,
            )
        )).scalar_one_or_none()
        if existing is None:
            existing = UserInterest(
                user_id=user.id,
                interest_type=sig.interest_type,
                interest_key=sig.interest_key,
                score=score,
                updated_at=now,
                expires_at=now + ttl,
            )
            db.add(existing)
        else:
            existing.score = max(existing.score, score)
            existing.updated_at = now
            existing.expires_at = now + ttl
    await db.commit()


@router.post("/compute", status_code=status.HTTP_200_OK)
@limiter.limit("30/minute")
async def compute_interests(payload: ComputeRequest, request: Request, response: Response):
    if not get_settings().personalization_enabled:
        raise HTTPException(status_code=404, detail="Not found", headers={"Cache-Control": "private, no-store"})
    if get_settings().personalization_purpose_required and not payload.consent:
        raise HTTPException(status_code=403, detail="Personalization consent required", headers={"Cache-Control": "private, no-store"})
    _no_store_headers(response)
    now = datetime.now(UTC)
    scores: dict[str, float] = {}
    for sig in payload.signals:
        if not _allowed(sig.interest_key, sig.interest_type):
            continue
        key = f"{sig.interest_type}:{sig.interest_key}"
        score = _decay(sig.occurred_at, now, get_settings().personalization_half_life_days)
        scores[key] = max(scores.get(key, 0.0), score)
    return {"scores": scores}


@router.delete("/interests", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def delete_interests(request: Request, response: Response, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    await db.execute(delete(UserInterest).where(UserInterest.user_id == user.id))
    await db.commit()


@router.get("/export", status_code=status.HTTP_200_OK)
@limiter.limit("30/minute")
async def export_interests(request: Request, response: Response, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (await db.execute(select(UserInterest).where(UserInterest.user_id == user.id))).scalars().all()
    return [{"interest_type": r.interest_type, "interest_key": r.interest_key, "score": r.score, "updated_at": r.updated_at.isoformat()} for r in rows]


@router.get("/picked-for-you", status_code=status.HTTP_200_OK)
@limiter.limit("60/minute")
async def picked_for_you(request: Request, response: Response):
    _no_store_headers(response)
    if not get_settings().personalization_enabled:
        raise HTTPException(status_code=404, detail="Not found", headers={"Cache-Control": "private, no-store"})
    return {"items": [], "reason": "No personalized items yet.", "reason_code": "no_signal"}


@router.get("/continue", status_code=status.HTTP_200_OK)
@limiter.limit("60/minute")
async def continue_where_left_off(request: Request, response: Response):
    _no_store_headers(response)
    if not get_settings().personalization_enabled:
        raise HTTPException(status_code=404, detail="Not found", headers={"Cache-Control": "private, no-store"})
    return {"items": [], "reason": "No recent activity.", "reason_code": "no_signal"}


@router.get("/ending-soon-saved", status_code=status.HTTP_200_OK)
@limiter.limit("60/minute")
async def ending_soon_saved(request: Request, response: Response):
    _no_store_headers(response)
    if not get_settings().personalization_enabled:
        raise HTTPException(status_code=404, detail="Not found", headers={"Cache-Control": "private, no-store"})
    return {"items": [], "reason": "No saved stores.", "reason_code": "no_signal"}


class SegmentRequest(BaseModel):
    consent: bool
    signals: list[Signal] = Field(default_factory=list, max_length=get_settings().personalization_max_signals)


@router.post("/segment", status_code=status.HTTP_200_OK)
@limiter.limit("30/minute")
async def segment_stateless(payload: SegmentRequest, request: Request, response: Response):
    if not get_settings().personalization_enabled:
        raise HTTPException(status_code=404, detail="Not found", headers={"Cache-Control": "private, no-store"})
    if get_settings().personalization_purpose_required and not payload.consent:
        raise HTTPException(status_code=403, detail="Personalization consent required", headers={"Cache-Control": "private, no-store"})
    _no_store_headers(response)
    now = datetime.now(UTC)
    scores: dict[str, float] = {}
    for sig in payload.signals:
        if not _allowed(sig.interest_key, sig.interest_type):
            continue
        key = f"{sig.interest_type}:{sig.interest_key}"
        score = _decay(sig.occurred_at, now, get_settings().personalization_half_life_days)
        scores[key] = max(scores.get(key, 0.0), score)
    return {"segment": compute_segment(scores), "version": get_settings().personalization_segment_version}


@router.get("/segment", status_code=status.HTTP_200_OK)
@limiter.limit("30/minute")
async def segment_logged_in(request: Request, response: Response, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    if not get_settings().personalization_enabled:
        raise HTTPException(status_code=404, detail="Not found", headers={"Cache-Control": "private, no-store"})
    if not user.personalization_consent:
        raise HTTPException(status_code=403, detail="Personalization consent required", headers={"Cache-Control": "private, no-store"})
    _no_store_headers(response)
    rows = (await db.execute(select(UserInterest).where(UserInterest.user_id == user.id))).scalars().all()
    scores = {f"{r.interest_type}:{r.interest_key}": r.score for r in rows}
    return {"segment": compute_segment(scores), "version": get_settings().personalization_segment_version}
