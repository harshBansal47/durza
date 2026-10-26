"""Alerts for stores a user follows and offers they track.

`alert_service` answers "did a product's price move?". This answers the other
question a coupon site is asked: "is there something new at the store I
follow, and is the offer I saved still good?". The two never overlap — they
read different tables and share only the senders.

Four things can happen, and each is worth one line in an email:

* ``new_offer``   — a followed store has a live offer created after the follow.
* ``confirmed``   — a tracked offer was verified working after it was saved.
* ``ending_soon`` — a tracked offer expires within the warning window.
* ``ended``       — a tracked offer expired, or stopped working.

Three properties this module is responsible for:

* **Derived, not streamed.** Nothing records "an offer appeared"; each scan
  looks at the current state. `DealAlertMark` is the memory that keeps a
  re-scan from re-sending, and a mark is written only after a channel accepted
  the send, so a failed delivery is retried rather than lost.
* **At most one email a day per person.** Everything pending goes into one
  digest. A site that mails every time anything changes teaches people to
  unsubscribe, which is the one outcome that costs more than the alert earned.
* **Nothing from the distant past.** Only changes inside a short window are
  reported, so turning the scheduler on after an outage does not write to
  people about last month.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.config import Settings, get_settings
from app.core.observability import get_logger
from app.core.timezones import ensure_utc
from app.models.coupon import Coupon, CouponStatus, VerificationStatus
from app.models.store import Store
from app.models.tracking import (
    AlertEvent,
    AlertKind,
    DealAlertMark,
    NotificationPreference,
    SavedCoupon,
    SavedStore,
)
from app.models.user import User
from app.services import notifications
from app.services.email import AlertContent, AlertItem
from app.services.push import PushSubscriptionGone

logger = get_logger("durza.deal_alerts")

DEAL_KINDS = (AlertKind.new_offer, AlertKind.confirmed, AlertKind.ending_soon, AlertKind.ended)

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

# After a failed send, wait before trying the same person again. Without it a
# mail outage would write a failed row for every user on every scan.
_RETRY_AFTER_FAILURE = timedelta(hours=2)


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


def _tracked_events(
    coupon: Coupon,
    saved_at: datetime,
    marked: set[tuple[uuid.UUID, str]],
    now: datetime,
    settings: Settings,
) -> list[DealEvent]:
    window_start = now - timedelta(days=settings.deal_alert_window_days)
    events: list[DealEvent] = []

    def pending(kind: AlertKind) -> bool:
        return (coupon.id, kind.value) not in marked

    if _is_live(coupon, now):
        if (
            coupon.verification_status == VerificationStatus.verified
            and coupon.last_verified_at is not None
            and ensure_utc(coupon.last_verified_at) > saved_at
            and ensure_utc(coupon.last_verified_at) > window_start
            and pending(AlertKind.confirmed)
        ):
            events.append(DealEvent(AlertKind.confirmed, coupon))
        if (
            coupon.expires_at is not None
            and ensure_utc(coupon.expires_at)
            <= now + timedelta(hours=settings.deal_alert_ending_soon_hours)
            and pending(AlertKind.ending_soon)
        ):
            events.append(DealEvent(AlertKind.ending_soon, coupon))
        return events

    gone = _gone_at(coupon, now)
    # `saved_at < gone`: tracking something that had already ended is not news.
    if gone is not None and saved_at < gone and gone > window_start and pending(AlertKind.ended):
        events.append(DealEvent(AlertKind.ended, coupon))
    return events


async def collect_events(db: AsyncSession, user_id: uuid.UUID, now: datetime) -> list[DealEvent]:
    settings = get_settings()
    window_start = now - timedelta(days=settings.deal_alert_window_days)
    events: list[DealEvent] = []

    # New offers at followed stores: created after the follow, live now, and
    # not already mentioned. The join lets one query apply each store's own
    # follow date.
    already_told = exists().where(
        DealAlertMark.user_id == user_id,
        DealAlertMark.coupon_id == Coupon.id,
        DealAlertMark.kind == AlertKind.new_offer.value,
    )
    new_offers = await db.execute(
        select(Coupon)
        .join(SavedStore, SavedStore.store_id == Coupon.store_id)
        .where(
            SavedStore.user_id == user_id,
            Coupon.is_active.is_(True),
            or_(Coupon.expires_at.is_(None), Coupon.expires_at > now),
            Coupon.created_at > SavedStore.created_at,
            Coupon.created_at > window_start,
            ~already_told,
        )
        .order_by(Coupon.created_at.desc())
        .limit(settings.deal_alert_max_items * 3)
    )
    events += [DealEvent(AlertKind.new_offer, c) for c in new_offers.scalars().all()]

    # Tracked offers: judged against what we already told this person.
    tracked = (
        await db.execute(
            select(Coupon, SavedCoupon.created_at)
            .join(SavedCoupon, SavedCoupon.coupon_id == Coupon.id)
            .where(SavedCoupon.user_id == user_id)
        )
    ).all()
    if tracked:
        marks = await db.execute(
            select(DealAlertMark.coupon_id, DealAlertMark.kind).where(
                DealAlertMark.user_id == user_id,
                DealAlertMark.coupon_id.in_([coupon.id for coupon, _ in tracked]),
            )
        )
        marked = {(coupon_id, kind) for coupon_id, kind in marks.all()}
        for coupon, saved_at in tracked:
            events += _tracked_events(coupon, ensure_utc(saved_at), marked, now, settings)

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
                    detail=f"{coupon.title}. You follow {store_name}, so we thought you would want to know.",
                    cta_label="See the offer",
                    cta_url=url,
                    store_name=store_name,
                )
            case AlertKind.confirmed:
                return AlertContent(
                    subject=f"Confirmed working: {coupon.title}",
                    preheader="We checked it and it works",
                    headline="This offer is confirmed working",
                    detail=f"{coupon.title}. You are tracking it, and we have now confirmed it works.",
                    cta_label="Use it now",
                    cta_url=url,
                    store_name=store_name,
                )
            case AlertKind.ending_soon:
                when = (
                    _hours_phrase(coupon.expires_at, now) if coupon.expires_at else "soon"
                )
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
        subject=f"{len(events)} updates on offers you follow",
        preheader=f"{shown[0].coupon.title}{more}",
        headline=f"{len(events)} updates on offers you follow",
        detail="Here is what changed since we last wrote to you." + (f" Showing the first {len(shown)}." if hidden else ""),
        cta_label="See everything",
        cta_url=f"{site}/account/saved",
        items=items,
    )


async def _recently(
    db: AsyncSession, user_id: uuid.UUID, *, delivered: bool, within: timedelta, now: datetime
) -> bool:
    """Has a deal alert (identified by its batch id) been recorded this recently?"""
    count = await db.scalar(
        select(func.count())
        .select_from(AlertEvent)
        .where(
            AlertEvent.user_id == user_id,
            AlertEvent.batch_id.is_not(None),
            AlertEvent.delivered.is_(delivered),
            AlertEvent.created_at > now - within,
        )
    )
    return bool(count)


async def evaluate_user(db: AsyncSession, user_id: uuid.UUID, now: datetime | None = None) -> int:
    """Send this person at most one digest. Returns the number of events it covered."""
    now = now or datetime.now(UTC)
    settings = get_settings()

    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        return 0

    prefs = await db.scalar(
        select(NotificationPreference).where(NotificationPreference.user_id == user_id)
    )
    if prefs is None:
        prefs = NotificationPreference(user_id=user_id)
        db.add(prefs)
        await db.flush()
    senders = notifications.senders_for(prefs)
    if not senders:
        return 0

    if await _recently(
        db,
        user_id,
        delivered=True,
        within=timedelta(hours=settings.deal_alert_min_hours_between_emails),
        now=now,
    ) or await _recently(db, user_id, delivered=False, within=_RETRY_AFTER_FAILURE, now=now):
        return 0

    events = await collect_events(db, user_id, now)
    if not events:
        return 0

    store_ids = {e.coupon.store_id for e in events}
    stores = {
        s.id: s for s in (await db.execute(select(Store).where(Store.id.in_(store_ids)))).scalars()
    }
    content = build_digest(events, stores, now)
    lead = events[0]

    batch_id = uuid.uuid4()
    delivered_any = False
    for sender in senders:
        try:
            delivered = await sender.send(user, prefs, content)
        except PushSubscriptionGone:
            # The browser unsubscribed; forget it now rather than on every send.
            prefs.push_subscription = None
            prefs.push_enabled = False
            delivered = False
            logger.info("cleared dead push subscription", extra={"user_id": str(user.id)})

        # Every attempt is recorded, delivered or not: the history is the audit
        # trail, and "I never got that email" has to be answerable.
        db.add(
            AlertEvent(
                user_id=user.id,
                tracked_product_id=None,
                kind=lead.kind,
                channel=sender.channel,
                detail=content.subject,
                delivered=delivered,
                coupon_id=lead.coupon.id,
                store_id=lead.coupon.store_id,
                batch_id=batch_id,
            )
        )
        metrics.alerts_sent.labels(sender.channel, "delivered" if delivered else "failed").inc()
        delivered_any = delivered_any or delivered

    if delivered_any:
        for event in events:
            db.add(
                DealAlertMark(user_id=user.id, coupon_id=event.coupon.id, kind=event.kind.value)
            )
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
    """Evaluate everyone who follows a store or tracks an offer. Returns digests sent."""
    followers = select(SavedStore.user_id)
    trackers = select(SavedCoupon.user_id)
    user_ids = (
        (
            await db.execute(
                select(User.id).where(
                    User.is_active.is_(True),
                    or_(User.id.in_(followers), User.id.in_(trackers)),
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
