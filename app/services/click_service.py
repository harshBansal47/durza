import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ad import Ad
from app.models.click import ClickEvent
from app.models.coupon import Coupon
from app.models.product import Product
from app.models.store import Store
from app.services import affiliate

# Two hits from the same visitor on the same target inside this window are one
# click (a double-tap, a refresh). The second one reuses the first's clickref so
# a network never sees two references for one purchase.
DEDUPE_WINDOW = timedelta(seconds=10)

_SRC_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_BOT_MARKERS = (
    "bot",
    "crawl",
    "spider",
    "slurp",
    "preview",
    "headless",
    "facebookexternalhit",
    "curl/",
    "wget/",
    "python-requests",
    "go-http-client",
)


def normalize_src(raw: str | None) -> str:
    """Only short, boring tags are accepted, so `src` can never smuggle markup or
    unbounded text into the table."""
    if raw:
        candidate = raw.strip().lower()
        if _SRC_RE.match(candidate):
            return candidate
    return "unknown"


def looks_like_bot(user_agent: str | None) -> bool:
    if not user_agent:
        return True
    ua = user_agent.lower()
    return any(marker in ua for marker in _BOT_MARKERS)


def new_clickref() -> str:
    return secrets.token_hex(16)  # 32 chars, fits String(32)


async def record_click(
    db: AsyncSession,
    *,
    store: Store | None,
    coupon: Coupon | None = None,
    product: Product | None = None,
    ad: Ad | None = None,
    src: str | None,
    visitor_hash: str,
    user_agent: str | None,
) -> tuple[ClickEvent, bool]:
    """Persist a click. Returns (event, is_new); `is_new` is False for a duplicate.

    The legacy `coupon.clicks_count` counter keeps counting every non-duplicate
    click (bots included, as before); `ClickEvent.is_bot` is what analytics use
    to separate them.
    """
    cutoff = datetime.now(UTC) - DEDUPE_WINDOW
    stmt = select(ClickEvent).where(
        ClickEvent.visitor_hash == visitor_hash,
        ClickEvent.created_at >= cutoff,
        ClickEvent.coupon_id == (coupon.id if coupon else None),
        ClickEvent.product_id == (product.id if product else None),
        ClickEvent.ad_id == (ad.id if ad else None),
    )
    existing = (await db.execute(stmt.limit(1))).scalar_one_or_none()
    if existing is not None:
        return existing, False

    is_bot = looks_like_bot(user_agent)
    event = ClickEvent(
        clickref=new_clickref(),
        coupon_id=coupon.id if coupon else None,
        product_id=product.id if product else None,
        ad_id=ad.id if ad else None,
        store_id=store.id if store else None,
        src=normalize_src(src),
        network=affiliate.normalize_network(store.affiliate_network if store else None),
        visitor_hash=visitor_hash,
        is_bot=is_bot,
        fraud_score=1.0 if is_bot else 0.0,
    )
    db.add(event)
    if coupon is not None:
        coupon.clicks_count += 1
    if ad is not None:
        ad.clicks_count += 1
    await db.commit()
    await db.refresh(event)
    return event, True


async def scrub_visitor_hashes(db: AsyncSession, *, older_than_days: int) -> int:
    """Privacy retention: forget who clicked, keep that a click happened.

    Revenue attribution only needs `clickref`, so the hash can go as soon as the
    dedupe window and any abuse investigation are over."""
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    result = await db.execute(
        update(ClickEvent)
        .where(ClickEvent.created_at < cutoff, ClickEvent.visitor_hash.is_not(None))
        .values(visitor_hash=None)
    )
    await db.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def get_store(db: AsyncSession, store_id: uuid.UUID) -> Store | None:
    return await db.get(Store, store_id)
