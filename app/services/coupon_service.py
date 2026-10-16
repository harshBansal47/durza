import enum
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import EntityNotFoundError
from app.core.pagination import paginate
from app.core.slugs import generate_unique_slug
from app.models.category import Category
from app.models.click import ClickEvent
from app.models.coupon import Coupon, DiscountType
from app.models.coupon_verification import CouponVerification
from app.models.store import Store
from app.schemas.coupon import CouponCreate, CouponUpdate

VERIFICATION_COOLDOWN = timedelta(hours=24)

# A coupon needs at least this many reports before its success rate is allowed
# to rank it: "100% (1 report)" must not beat "94% (48 reports)".
MIN_REPORTS_TO_RANK = 5
TRENDING_WINDOW = timedelta(days=7)


class CouponSort(str, enum.Enum):
    """Orderings the storefront may ask for. Each is backed by data we hold; the
    frontend presents them and never ranks anything itself.

    newest             most recently added (default, the historical behaviour)
    recommended        the ranking engine's quality score (app.services.quality), best first
    ending_soon        soonest real expiry first; coupons with no expiry excluded
    recently_verified  most recently confirmed by a shopper; never-verified excluded
    trending           most real (non-bot) outbound clicks in the last 7 days; coupons
                       nobody clicked this week are excluded rather than padded in
    most_used          most all-time clicks (explicitly NOT "trending")
    top_rated          highest success rate among coupons with enough reports
    biggest_discount   highest percentage off. Percentage coupons only: a fixed amount
                       ("500 off") and a percentage cannot be compared without
                       knowing the basket, so fixed and code-less deals are excluded
                       rather than ranked on an invented scale
    """

    newest = "newest"
    recommended = "recommended"
    ending_soon = "ending_soon"
    recently_verified = "recently_verified"
    trending = "trending"
    most_used = "most_used"
    top_rated = "top_rated"
    biggest_discount = "biggest_discount"


async def list_coupons(
    db: AsyncSession,
    *,
    store_id: uuid.UUID | None = None,
    category_id: uuid.UUID | None = None,
    search: str | None = None,
    active_only: bool = True,
    sort: CouponSort = CouponSort.newest,
    discount_type: DiscountType | None = None,
    has_code: bool | None = None,
    verified: bool | None = None,
    min_success_rate: float | None = None,
    expires_within_days: int | None = None,
    verified_within_hours: int | None = None,
    skip: int = 0,
    limit: int = 20,
) -> tuple[Sequence[Coupon], int]:
    """Listing filters the storefront offers.

    Every one of these narrows the row set *before* pagination, so `total` is
    the exact number of matches and a filtered page can be paged through. The
    storefront never filters in the browser: a client-side filter over one
    page of results reports a count that is wrong for anything past page one.

    `verified` ("somebody has confirmed this at least once") and
    `verified_within_hours` ("confirmed within the last N hours") are separate
    questions and both are asked often: the first is a trust signal, the second
    is recency.
    """
    now = datetime.now(UTC)
    stmt = select(Coupon)
    if store_id is not None:
        stmt = stmt.where(Coupon.store_id == store_id)
    if category_id is not None:
        stmt = stmt.where(Coupon.category_id == category_id)
    if search:
        stmt = stmt.where(Coupon.title.ilike(f"%{search}%"))
    if discount_type is not None:
        stmt = stmt.where(Coupon.discount_type == discount_type)
    if has_code is not None:
        # NOT: an empty string is a real code-less deal, not "no code given".
        stmt = stmt.where(Coupon.code.is_not(None) if has_code else Coupon.code.is_(None))
    if verified is not None:
        stmt = stmt.where(
            Coupon.last_verified_at.is_not(None) if verified else Coupon.last_verified_at.is_(None)
        )
    if min_success_rate is not None:
        # A rate with no reports behind it is not a rate, so never-verified
        # coupons cannot pass this filter however high the threshold is.
        reports = Coupon.success_count + Coupon.fail_count
        stmt = stmt.where(reports > 0, (Coupon.success_count * 1.0) / reports >= min_success_rate)
    if active_only:
        # The lifecycle job deactivates expired coupons on a schedule; this makes
        # "active" true the instant a coupon expires, not at the next run.
        stmt = stmt.where(
            Coupon.is_active.is_(True),
            or_(Coupon.expires_at.is_(None), Coupon.expires_at > now),
        )
    if expires_within_days is not None:
        stmt = stmt.where(
            Coupon.expires_at.is_not(None),
            Coupon.expires_at > now,
            Coupon.expires_at <= now + timedelta(days=expires_within_days),
        )

    if verified_within_hours is not None:
        stmt = stmt.where(
            Coupon.last_verified_at.is_not(None),
            Coupon.last_verified_at >= now - timedelta(hours=verified_within_hours),
        )

    if sort is CouponSort.recommended:
        # The score is computed and stored by the ranking engine; this only reads it.
        stmt = stmt.order_by(Coupon.quality_score.desc(), Coupon.created_at.desc())
    elif sort is CouponSort.ending_soon:
        stmt = stmt.where(Coupon.expires_at.is_not(None), Coupon.expires_at > now)
        stmt = stmt.order_by(Coupon.expires_at.asc(), Coupon.created_at.desc())
    elif sort is CouponSort.recently_verified:
        stmt = stmt.where(Coupon.last_verified_at.is_not(None))
        stmt = stmt.order_by(Coupon.last_verified_at.desc(), Coupon.created_at.desc())
    elif sort is CouponSort.biggest_discount:
        stmt = stmt.where(
            Coupon.discount_type == DiscountType.percentage,
            Coupon.discount_value.is_not(None),
        )
        stmt = stmt.order_by(Coupon.discount_value.desc(), Coupon.created_at.desc())
    elif sort is CouponSort.most_used:
        stmt = stmt.order_by(Coupon.clicks_count.desc(), Coupon.created_at.desc())
    elif sort is CouponSort.top_rated:
        reports = Coupon.success_count + Coupon.fail_count
        stmt = stmt.where(reports >= MIN_REPORTS_TO_RANK)
        rate = (Coupon.success_count * 1.0) / reports
        stmt = stmt.order_by(rate.desc(), reports.desc(), Coupon.created_at.desc())
    elif sort is CouponSort.trending:
        recent = (
            select(ClickEvent.coupon_id.label("cid"), func.count().label("n"))
            .where(
                ClickEvent.created_at >= now - TRENDING_WINDOW,
                ClickEvent.is_bot.is_(False),
                ClickEvent.coupon_id.is_not(None),
            )
            .group_by(ClickEvent.coupon_id)
            .subquery()
        )
        stmt = stmt.join(recent, recent.c.cid == Coupon.id)
        stmt = stmt.order_by(recent.c.n.desc(), Coupon.created_at.desc())
    else:
        stmt = stmt.order_by(Coupon.created_at.desc())
    return await paginate(db, stmt, skip, limit)


async def get_coupon(db: AsyncSession, coupon_id: uuid.UUID) -> Coupon | None:
    return await db.get(Coupon, coupon_id)


async def get_coupon_by_slug(db: AsyncSession, slug: str) -> Coupon | None:
    result = await db.execute(select(Coupon).where(Coupon.slug == slug))
    return result.scalar_one_or_none()


async def _ensure_store_and_category(
    db: AsyncSession, store_id: uuid.UUID | None, category_id: uuid.UUID | None
) -> None:
    """Give a clean 404 instead of a raw foreign-key error from the database."""
    if store_id is not None and await db.get(Store, store_id) is None:
        raise EntityNotFoundError(f"Store {store_id} not found")
    if category_id is not None and await db.get(Category, category_id) is None:
        raise EntityNotFoundError(f"Category {category_id} not found")


async def create_coupon(db: AsyncSession, payload: CouponCreate, created_by: uuid.UUID) -> Coupon:
    await _ensure_store_and_category(db, payload.store_id, payload.category_id)

    coupon = Coupon(
        title=payload.title,
        slug=await generate_unique_slug(db, Coupon, payload.title),
        code=payload.code,
        description=payload.description,
        discount_type=payload.discount_type,
        discount_value=payload.discount_value,
        store_id=payload.store_id,
        category_id=payload.category_id,
        destination_url=payload.destination_url,
        expires_at=payload.expires_at,
        created_by=created_by,
    )
    db.add(coupon)
    await db.commit()
    await db.refresh(coupon)
    return coupon


async def update_coupon(db: AsyncSession, coupon: Coupon, payload: CouponUpdate) -> Coupon:
    data = payload.model_dump(exclude_unset=True)
    await _ensure_store_and_category(db, data.get("store_id"), data.get("category_id"))

    if "title" in data and data["title"] != coupon.title:
        data["slug"] = await generate_unique_slug(db, Coupon, data["title"], exclude_id=coupon.id)

    for field, value in data.items():
        setattr(coupon, field, value)
    await db.commit()
    await db.refresh(coupon)
    return coupon


async def delete_coupon(db: AsyncSession, coupon: Coupon) -> None:
    await db.delete(coupon)
    await db.commit()


async def record_click(db: AsyncSession, coupon: Coupon) -> None:
    coupon.clicks_count += 1
    await db.commit()


class AlreadyVerifiedRecentlyError(Exception):
    """Raised when the same visitor already voted on this coupon within the cooldown."""


async def record_verification(
    db: AsyncSession,
    coupon: Coupon,
    *,
    worked: bool,
    ip_hash: str,
    note: str | None = None,
) -> Coupon:
    cutoff = datetime.now(UTC) - VERIFICATION_COOLDOWN
    recent = await db.execute(
        select(CouponVerification).where(
            CouponVerification.coupon_id == coupon.id,
            CouponVerification.ip_hash == ip_hash,
            CouponVerification.created_at >= cutoff,
        )
    )
    if recent.scalar_one_or_none() is not None:
        raise AlreadyVerifiedRecentlyError

    db.add(
        CouponVerification(coupon_id=coupon.id, ip_hash=ip_hash, worked=worked, note=note or None)
    )
    if worked:
        coupon.success_count += 1
    else:
        coupon.fail_count += 1
    coupon.last_verified_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(coupon)
    return coupon


async def get_verification_history(
    db: AsyncSession, coupon_id: uuid.UUID, limit: int = 50
) -> Sequence[CouponVerification]:
    result = await db.execute(
        select(CouponVerification)
        .where(CouponVerification.coupon_id == coupon_id)
        .order_by(CouponVerification.created_at.desc())
        .limit(limit)
    )
    return result.scalars().all()
