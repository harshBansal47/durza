import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import paginate
from app.models.ad import Ad, AdPosition
from app.schemas.ad import AdCreate, AdUpdate


async def list_ads(
    db: AsyncSession,
    *,
    position: AdPosition | None = None,
    active_only: bool = True,
    skip: int = 0,
    limit: int = 20,
) -> tuple[Sequence[Ad], int]:
    stmt = select(Ad)
    if position is not None:
        stmt = stmt.where(Ad.position == position)
    if active_only:
        # "Active" means switched on AND inside its booked window. Without the
        # window check an expired paid campaign would keep serving forever.
        now = datetime.now(UTC)
        stmt = stmt.where(
            Ad.is_active.is_(True),
            or_(Ad.starts_at.is_(None), Ad.starts_at <= now),
            or_(Ad.ends_at.is_(None), Ad.ends_at > now),
        )
    return await paginate(db, stmt.order_by(Ad.created_at.desc()), skip, limit)


async def get_ad(db: AsyncSession, ad_id: uuid.UUID) -> Ad | None:
    return await db.get(Ad, ad_id)


async def create_ad(db: AsyncSession, payload: AdCreate) -> Ad:
    ad = Ad(**payload.model_dump())
    db.add(ad)
    await db.commit()
    await db.refresh(ad)
    return ad


async def update_ad(db: AsyncSession, ad: Ad, payload: AdUpdate) -> Ad:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(ad, field, value)
    await db.commit()
    await db.refresh(ad)
    return ad


async def delete_ad(db: AsyncSession, ad: Ad) -> None:
    await db.delete(ad)
    await db.commit()


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def is_live(ad: Ad, now: datetime | None = None) -> bool:
    """True when the ad is switched on and today falls inside its window."""
    now = now or datetime.now(UTC)
    starts, ends = _aware(ad.starts_at), _aware(ad.ends_at)
    return ad.is_active and (starts is None or starts <= now) and (ends is None or ends > now)


def build_target_url(target_url: str, ad_id: uuid.UUID) -> str | None:
    """Advertiser URL with UTM tags so the advertiser can see Couponbase traffic.
    Existing UTM values the advertiser set are kept. Returns None for anything
    that is not an http(s) URL (legacy rows saved before validation existed)."""
    parts = urlparse(target_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    query = parse_qsl(parts.query, keep_blank_values=True)
    present = {k for k, _ in query}
    for key, value in (
        ("utm_source", "couponbase"),
        ("utm_medium", "sponsored"),
        ("utm_campaign", str(ad_id)),
    ):
        if key not in present:
            query.append((key, value))
    return urlunparse(parts._replace(query=urlencode(query)))
