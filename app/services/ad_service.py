import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import paginate
from app.core.timezones import ensure_utc_optional
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


def is_live(ad: Ad, now: datetime | None = None) -> bool:
    """True when the ad is switched on and today falls inside its window.

    The window columns are compared through `ensure_utc`, which matters because
    `now` is always aware and a `DateTime(timezone=True)` column read back from
    SQLite is not. Without the coercion this raises `TypeError: can't compare
    offset-naive and offset-aware datetimes` on SQLite and silently works on
    Postgres — the kind of difference a test suite should not be the thing that
    discovers.

    Naive therefore means UTC, which is correct because that is the only value we
    ever write. It is not, and cannot be, "local time": a naive datetime carries
    no offset, so `datetime.now()`'s value cannot be recovered from it.
    """
    now = now or datetime.now(UTC)
    starts = ensure_utc_optional(ad.starts_at)
    ends = ensure_utc_optional(ad.ends_at)
    return ad.is_active and (starts is None or starts <= now) and (ends is None or ends > now)


def build_target_url(target_url: str, ad_id: uuid.UUID) -> str | None:
    """Advertiser URL with UTM tags so the advertiser can see Durza traffic.
    Existing UTM values the advertiser set are kept. Returns None for anything
    that is not an http(s) URL (legacy rows saved before validation existed)."""
    parts = urlparse(target_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    query = parse_qsl(parts.query, keep_blank_values=True)
    present = {k for k, _ in query}
    for key, value in (
        ("utm_source", "durza"),
        ("utm_medium", "sponsored"),
        ("utm_campaign", str(ad_id)),
    ):
        if key not in present:
            query.append((key, value))
    return urlunparse(parts._replace(query=urlencode(query)))
