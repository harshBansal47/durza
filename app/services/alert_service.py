"""Evaluate tracked products against their latest observation and fire alerts.

Called by the scheduler (and by `scripts/run_alerts.py` when you want a manual
run). Sends at most one alert per (tracked product, price point) pair — either
the target-met condition, a price drop, or a new coupon, in that priority
order.

Two reliability properties this module is responsible for:

* **One alert per observation.** `TrackAlertState` remembers the last price
  point we told the user about, so a re-scan over unchanged data sends nothing.
  Without it, every scheduler tick would re-email everyone.
* **A dead push subscription is forgotten.** A browser that unsubscribed gets a
  410 on every send; those rows are cleared rather than retried forever.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.config import get_settings
from app.core.observability import get_logger
from app.models.coupon import Coupon
from app.models.product import PricePoint, Product
from app.models.tracking import (
    AlertEvent,
    AlertKind,
    NotificationPreference,
    TrackAlertState,
    TrackedProduct,
)
from app.models.user import User
from app.services import notifications
from app.services.email import AlertContent
from app.services.price_service import effective_price
from app.services.push import PushSubscriptionGone

logger = get_logger("couponza.alerts")


def _money(amount: float, currency: str) -> str:
    """Format for an email subject / push body, where Intl is unavailable.

    Deliberately crude: `1,299.00` reads correctly in every locale, whereas a
    locale-formatted string in a push notification can arrive mangled.
    """
    return f"{amount:,.2f} {currency}"


def _product_url(slug: str) -> str:
    return f"{get_settings().site_url.rstrip('/')}/products/{slug}"


async def _get_or_create_state(db: AsyncSession, tracked_id: uuid.UUID) -> TrackAlertState:
    result = await db.execute(
        select(TrackAlertState).where(TrackAlertState.tracked_product_id == tracked_id)
    )
    state = result.scalar_one_or_none()
    if state is None:
        state = TrackAlertState(tracked_product_id=tracked_id)
        db.add(state)
        await db.flush()
    return state


def _build_alert(
    kind: AlertKind,
    product: Product,
    latest: PricePoint,
    previous: PricePoint | None,
    coupon: Coupon | None,
    target: float | None,
) -> AlertContent:
    """Turn a decision into something a person can act on.

    The subject line is the whole message in a notification list, so it carries
    the product and the new price rather than a category like "price drop".
    """
    eff = effective_price(latest, coupon)
    url = _product_url(product.slug)
    price_line = _money(float(latest.price), product.currency)
    if eff < float(latest.price):
        price_line = f"{price_line} → {_money(eff, product.currency)} after the code"

    match kind:
        case AlertKind.target_met:
            return AlertContent(
                subject=f"{product.name} reached your target of {_money(target, product.currency)}",
                preheader=f"Now {price_line}",
                headline="Your target was met",
                detail=(
                    f"{product.name} is now {_money(eff, product.currency)}, at or below the "
                    f"{_money(target, product.currency)} you asked us to watch for."
                ),
                cta_label="See the price",
                cta_url=url,
                store_name=None,
                product_name=product.name,
                price_line=price_line,
                code=coupon.code if coupon else None,
            )
        case AlertKind.price_drop:
            drop = float(previous.price) - float(latest.price) if previous else 0.0
            return AlertContent(
                subject=f"{product.name} dropped to {_money(latest.price, product.currency)}",
                preheader=f"Down {_money(drop, product.currency)}",
                headline="A price drop",
                detail=(
                    f"{product.name} fell from {_money(float(previous.price), product.currency)} "
                    f"to {_money(float(latest.price), product.currency)}."
                    if previous
                    else f"{product.name} is now {_money(float(latest.price), product.currency)}."
                ),
                cta_label="See the price",
                cta_url=url,
                product_name=product.name,
                price_line=price_line,
                code=coupon.code if coupon else None,
            )
        case AlertKind.coupon_appeared:
            return AlertContent(
                subject=f"A code appeared for {product.name}",
                preheader=f"Effective price {_money(eff, product.currency)}",
                headline="A new code applies",
                detail=(
                    f"{coupon.code} is now valid on {product.name}, bringing the effective price "
                    f"to {_money(eff, product.currency)}."
                    if coupon and coupon.code
                    else f"A discount now applies to {product.name}, bringing the effective price "
                    f"to {_money(eff, product.currency)}."
                ),
                cta_label="See the deal",
                cta_url=url,
                product_name=product.name,
                price_line=price_line,
                code=coupon.code if coupon else None,
            )


async def evaluate_tracked_product(db: AsyncSession, tracked: TrackedProduct) -> list[AlertEvent]:
    points = (
        (
            await db.execute(
                select(PricePoint)
                .where(PricePoint.product_id == tracked.product_id)
                .order_by(PricePoint.captured_at.desc())
                .limit(2)
            )
        )
        .scalars()
        .all()
    )
    if not points:
        return []
    latest = points[0]
    previous = points[1] if len(points) > 1 else None

    state = await _get_or_create_state(db, tracked.id)
    if state.last_price_point_id == latest.id and state.alerts_sent > 0:
        return []  # already told this user about this observation

    product = await db.get(Product, tracked.product_id)
    coupon = await db.get(Coupon, latest.coupon_id) if latest.coupon_id else None
    if product is None:
        return []

    # Priority: cheapest-legitimate-price signal beats a mere price drop beats
    # a new coupon. One alert per observation keeps users from being spammed.
    eff = effective_price(latest, coupon)
    kind: AlertKind | None = None

    if tracked.target_price is not None and eff <= float(tracked.target_price):
        kind = AlertKind.target_met
    elif previous is not None and float(latest.price) < float(previous.price):
        kind = AlertKind.price_drop
    elif latest.coupon_id is not None and (previous is None or previous.coupon_id is None):
        kind = AlertKind.coupon_appeared

    if kind is None:
        state.last_price_point_id = latest.id
        await db.commit()
        return []

    user = await db.get(User, tracked.user_id)
    if user is None:
        return []
    prefs_result = await db.execute(
        select(NotificationPreference).where(NotificationPreference.user_id == user.id)
    )
    prefs = prefs_result.scalar_one_or_none()
    if prefs is None:
        prefs = NotificationPreference(user_id=user.id)
        db.add(prefs)
        await db.flush()

    alert = _build_alert(kind, product, latest, previous, coupon, tracked.target_price)

    events: list[AlertEvent] = []
    dead_subscriptions = 0
    for sender in notifications.senders_for(prefs):
        try:
            delivered = await sender.send(user, prefs, alert)
        except PushSubscriptionGone:
            # The browser unsubscribed. Forget it now rather than on every
            # future alert — this is the only place with a session to do it in.
            prefs.push_subscription = None
            prefs.push_enabled = False
            dead_subscriptions += 1
            delivered = False
            logger.info(
                "cleared dead push subscription",
                extra={"user_id": str(user.id), "tracked_product_id": str(tracked.id)},
            )

        # Every attempt is recorded, delivered or not. The alert history is the
        # audit trail; if we only logged successes, a user reporting "I never got
        # that email" would be unfalsifiable.
        event = AlertEvent(
            user_id=user.id,
            tracked_product_id=tracked.id,
            kind=kind,
            channel=sender.channel,
            detail=alert.subject,
            delivered=delivered,
            price_point_id=latest.id,
        )
        db.add(event)
        events.append(event)
        metrics.alerts_sent.labels(sender.channel, "delivered" if delivered else "failed").inc()

        if not delivered and sender.channel != "push":
            logger.warning(
                "alert channel failed",
                extra={
                    "channel": sender.channel,
                    "user_id": str(user.id),
                    "tracked_product_id": str(tracked.id),
                },
            )

    state.last_price_point_id = latest.id
    state.last_alert_kind = kind.value
    state.alerts_sent += len(events)
    await db.commit()

    logger.info(
        "alert dispatched",
        extra={
            "kind": kind.value,
            "channels": [e.channel for e in events],
            "tracked_product_id": str(tracked.id),
            "dead_subscriptions_cleared": dead_subscriptions,
        },
    )
    return events


async def run_alert_scan(db: AsyncSession) -> int:
    result = await db.execute(select(TrackedProduct))
    total = 0
    for tracked in result.scalars().all():
        try:
            total += len(await evaluate_tracked_product(db, tracked))
        except Exception:
            # One broken tracked product must not abort the sweep for everyone
            # else — that is the difference between an alert system and an
            # outage generator.
            logger.exception(
                "failed to evaluate tracked product",
                extra={"tracked_product_id": str(tracked.id)},
            )
    return total
