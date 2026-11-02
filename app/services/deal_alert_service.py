"""Email digests derived from stores and coupons a user has saved.

This service answers "is there something new at a store I follow, and did a
saved offer change?".

Four things can happen, and each is worth one line in an email:

* ``new_offer``   — a followed store has a live offer created since the last scan.
* ``confirmed``   — a saved offer was verified working since the last scan.
* ``ending_soon`` — a saved offer entered the warning window since the last scan.
* ``ended``       — a saved offer expired, or stopped working, since the last scan.

Three properties this module is responsible for:

* **Digest.** Changes are collected into one email per subscriber per scan.
* **No user alert history.** The scan interval is the only delivery window; the
  service stores no per-user alert records.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.config import Settings, get_settings
from app.core.observability import get_logger
from app.core.timezones import ensure_utc
from app.models.coupon import Coupon, CouponStatus, VerificationStatus
from app.models.store import Store
from app.models.saved_items import (
    AlertKind,
    NotificationPreference,
    SavedCoupon,
    SavedStore,
)
from app.models.user import User
from app.services import notifications
from app.services.email import AlertContent, AlertItem

logger = get_logger("durza.deal_alerts")

# A coupon in one of these states has stopped being usable. `pending_validation`
# is deliberately absent: an offer that has not gone live yet has not ended.
_DEAD_STATUSES = frozenset(
    {
        CouponStatus.expired,
        CouponStatus.failed,
        CouponStatus.validation_failed,
        CouponStatus.source_removed,
        CouponStatus.disabled,
    }
)

# Most urgent first: an offer that is gone matters more than one that is new.
_PRIORITY = {
    AlertKind.ended: 0,
    AlertKind.ending_soon: 1,
    AlertKind.confirmed: 2,
    AlertKind.new_offer: 3,
}

_LABEL = {
    AlertKind.ended: "No longer available",
    AlertKind.ending_soon: "Ending soon",
    AlertKind.confirmed: "Confirmed working",
    AlertKind.new_offer: "New offer",
}

@dataclass(frozen=True)
class DealEvent:
    kind: AlertKind
    coupon: Coupon


def _is_live(coupon: Coupon, now: datetime) -> bool:
    """The public site's own rule for "this offer can be used right now"."""
    return bool(coupon.is_active) and (
        coupon.expires_at is None or ensure_utc(coupon.expires_at) > now
    )


def _gone_at(coupon: Coupon, now: datetime) -> datetime | None:
    """When a coupon stopped being usable, or None if it still is (or never started)."""
    if coupon.expires_at is not None and ensure_utc(coupon.expires_at) <= now:
        return ensure_utc(coupon.expires_at)
    if coupon.status in _DEAD_STATUSES:
        return ensure_utc(coupon.updated_at)
    return None


def _saved_coupon_events(
    coupon: Coupon,
    saved_at: datetime,
    now: datetime,
    settings: Settings,
) -> list[DealEvent]:
    window_start = now - timedelta(minutes=settings.alert_scan_interval_minutes)
    events: list[DealEvent] = []

    if _is_live(coupon, now):
        verified_at = ensure_utc(coupon.last_verified_at) if coupon.last_verified_at else None
        if (
            coupon.verification_status == VerificationStatus.verified
            and verified_at is not None
            and saved_at < verified_at <= now
            and window_start < verified_at
        ):
            events.append(DealEvent(AlertKind.confirmed, coupon))
        if coupon.expires_at is not None:
            expires_at = ensure_utc(coupon.expires_at)
            warning_started_at = expires_at - timedelta(
                hours=settings.deal_alert_ending_soon_hours
            )
            if saved_at < warning_started_at and window_start < warning_started_at <= now:
                events.append(DealEvent(AlertKind.ending_soon, coupon))
        return events

    gone = _gone_at(coupon, now)
    if gone is not None and saved_at < gone and window_start < gone <= now:
        events.append(DealEvent(AlertKind.ended, coupon))
    return events


async def collect_events(db: AsyncSession, user_id: uuid.UUID, now: datetime) -> list[DealEvent]:
    settings = get_settings()
    window_start = now - timedelta(minutes=settings.alert_scan_interval_minutes)
    events: list[DealEvent] = []

    # New offers at followed stores: created after the follow, live now, and
    # created during this scan interval. The join applies each store's follow date.
    new_offers = await db.execute(
        select(Coupon)
        .join(SavedStore, SavedStore.store_id == Coupon.store_id)
        .where(
            SavedStore.user_id == user_id,
            Coupon.is_active.is_(True),
            or_(Coupon.expires_at.is_(None), Coupon.expires_at > now),
            Coupon.created_at > SavedStore.created_at,
            Coupon.created_at > window_start,
            Coupon.created_at <= now,
        )
        .order_by(Coupon.created_at.desc())
        .limit(settings.deal_alert_max_items * 3)
    )
    events += [DealEvent(AlertKind.new_offer, c) for c in new_offers.scalars().all()]

    # Saved offers: only changes since the previous scan are eligible.
    saved_coupons = (
        await db.execute(
            select(Coupon, SavedCoupon.created_at)
            .join(SavedCoupon, SavedCoupon.coupon_id == Coupon.id)
            .where(SavedCoupon.user_id == user_id)
        )
    ).all()
    for coupon, saved_at in saved_coupons:
        events += _saved_coupon_events(coupon, ensure_utc(saved_at), now, settings)

    # One mention per offer. If the same offer is both "confirmed" and "ending
    # soon", say the more urgent thing.
    best: dict[uuid.UUID, DealEvent] = {}
    for event in events:
        current = best.get(event.coupon.id)
        if current is None or _PRIORITY[event.kind] < _PRIORITY[current.kind]:
            best[event.coupon.id] = event
    return sorted(best.values(), key=lambda e: (_PRIORITY[e.kind], e.coupon.title))


def _offer_url(slug: str) -> str:
    return f"{get_settings().site_url.rstrip('/')}/coupons/{slug}"


def _hours_phrase(expires_at: datetime, now: datetime) -> str:
    hours = max(1, int((ensure_utc(expires_at) - now).total_seconds() // 3600))
    if hours < 24:
        return f"in about {hours} hour{'s' if hours != 1 else ''}"
    days = round(hours / 24)
    return f"in about {days} day{'s' if days != 1 else ''}"


def build_digest(
    events: list[DealEvent], stores: dict[uuid.UUID, Store], now: datetime
) -> AlertContent:
    """One email for everything pending. A lone event gets a message of its own."""
    settings = get_settings()
    site = settings.site_url.rstrip("/")
    shown = events[: settings.deal_alert_max_items]
    hidden = len(events) - len(shown)

    if len(events) == 1:
        event = events[0]
        coupon = event.coupon
        store = stores.get(coupon.store_id)
        store_name = store.name if store else "the store"
        url = _offer_url(coupon.slug)
        match event.kind:
            case AlertKind.new_offer:
                return AlertContent(
                    subject=f"New at {store_name}: {coupon.title}",
                    preheader=coupon.title,
                    headline=f"A new offer at {store_name}",
                    detail=f"{coupon.title}. You saved {store_name}, so we thought you would want to know.",
                    cta_label="See the offer",
                    cta_url=url,
                    store_name=store_name,
                )
            case AlertKind.confirmed:
                return AlertContent(
                    subject=f"Confirmed working: {coupon.title}",
                    preheader="We checked it and it works",
                    headline="This offer is confirmed working",
                    detail=f"{coupon.title}. You saved it, and we have now confirmed it works.",
                    cta_label="Use it now",
                    cta_url=url,
                    store_name=store_name,
                )
            case AlertKind.ending_soon:
                when = _hours_phrase(coupon.expires_at, now) if coupon.expires_at else "soon"
                return AlertContent(
                    subject=f"Ending soon: {coupon.title}",
                    preheader=f"Ends {when}",
                    headline="This offer is about to end",
                    detail=f"{coupon.title} ends {when}.",
                    cta_label="Use it before it ends",
                    cta_url=url,
                    store_name=store_name,
                )
            case _:
                return AlertContent(
                    subject=f"No longer available: {coupon.title}",
                    preheader="It has ended or stopped working",
                    headline="This offer is no longer available",
                    detail=(
                        f"{coupon.title} has ended or stopped working. "
                        f"There may be a newer offer at {store_name}."
                    ),
                    cta_label=f"See {store_name} offers" if store else "See offers",
                    cta_url=f"{site}/stores/{store.slug}" if store else site,
                    store_name=store_name,
                )

    items = tuple(
        AlertItem(label=_LABEL[e.kind], title=e.coupon.title, url=_offer_url(e.coupon.slug))
        for e in shown
    )
    more = f" and {hidden} more" if hidden else ""
    return AlertContent(
        subject=f"{len(events)} updates on saved offers",
        preheader=f"{shown[0].coupon.title}{more}",
        headline=f"{len(events)} updates on saved offers",
        detail="Here is what changed in offers you saved."
        + (f" Showing the first {len(shown)}." if hidden else ""),
        cta_label="See everything",
        cta_url=f"{site}/account/saved",
        items=items,
    )


async def evaluate_user(db: AsyncSession, user_id: uuid.UUID, now: datetime | None = None) -> int:
    """Send one digest for pending offers; returns the number covered."""
    now = now or datetime.now(UTC)

    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        return 0

    prefs = await db.scalar(
        select(NotificationPreference).where(NotificationPreference.user_id == user_id)
    )
    if prefs is None:
        prefs = NotificationPreference(user_id=user_id)
        db.add(prefs)
        await db.commit()
    senders = notifications.senders_for(prefs)
    if not senders:
        return 0

    events = await collect_events(db, user_id, now)
    if not events:
        return 0

    store_ids = {e.coupon.store_id for e in events}
    stores = {
        s.id: s for s in (await db.execute(select(Store).where(Store.id.in_(store_ids)))).scalars()
    }
    content = build_digest(events, stores, now)

    delivered_any = False
    for sender in senders:
        delivered = await sender.send(user, prefs, content)
        metrics.alerts_sent.labels(sender.channel, "delivered" if delivered else "failed").inc()
        delivered_any = delivered_any or delivered

    await db.commit()

    logger.info(
        "deal alert dispatched",
        extra={
            "user_id": str(user.id),
            "events": len(events),
            "delivered": delivered_any,
            "kinds": sorted({e.kind.value for e in events}),
        },
    )
    return len(events) if delivered_any else 0


async def run_deal_alert_scan(db: AsyncSession) -> int:
    """Evaluate users with saved stores or coupons. Returns digests sent."""
    followers = select(SavedStore.user_id)
    saved_coupons = select(SavedCoupon.user_id)
    user_ids = (
        (
            await db.execute(
                select(User.id).where(
                    User.is_active.is_(True),
                    or_(User.id.in_(followers), User.id.in_(saved_coupons)),
                )
            )
        )
        .scalars()
        .all()
    )

    sent = 0
    for user_id in user_ids:
        try:
            if await evaluate_user(db, user_id):
                sent += 1
        except Exception:
            # One person's bad data must not stop everyone else's alerts.
            await db.rollback()
            logger.exception("failed to evaluate deal alerts", extra={"user_id": str(user_id)})
    return sent
