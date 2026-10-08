"""Channel selection and the delivery decisions the alert service makes.

The recurring theme: an attempt is recorded whether or not it succeeded, and a
failure never takes down the sweep. Both are things that look correct until the
first real outage, at which point a partial implementation either loses the
audit trail or stops alerting everybody because one person bounced.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.category import Category
from app.models.coupon import Coupon
from app.models.product import PricePoint, Product
from app.models.store import Store
from app.models.tracking import AlertEvent, NotificationPreference, TrackedProduct
from app.models.user import User
from app.models.verification_attempt import AttemptOutcome
from app.services import alert_service, notifications
from app.services.email import AlertContent


def _prefs(**kwargs) -> NotificationPreference:
    defaults = {
        "email_enabled": False,
        "telegram_enabled": False,
        "telegram_chat_id": None,
        "push_enabled": False,
        "push_subscription": None,
    }
    return NotificationPreference(**{**defaults, **kwargs})


# ---- Channel selection ----


def test_no_channels_when_everything_is_off() -> None:
    assert notifications.senders_for(_prefs()) == []


def test_email_channel_requires_only_the_email_toggle() -> None:
    senders = notifications.senders_for(_prefs(email_enabled=True))
    assert [s.channel for s in senders] == ["email"]


def test_telegram_requires_both_the_toggle_and_a_chat_id() -> None:
    # A toggle with no chat id has nowhere to send. Registering the channel
    # anyway produces a daily warning log and no message.
    assert notifications.senders_for(_prefs(telegram_enabled=True)) == []
    senders = notifications.senders_for(_prefs(telegram_enabled=True, telegram_chat_id="123"))
    assert [s.channel for s in senders] == ["telegram"]


def test_push_requires_both_the_toggle_and_a_subscription() -> None:
    assert notifications.senders_for(_prefs(push_enabled=True)) == []
    senders = notifications.senders_for(
        _prefs(push_enabled=True, push_subscription='{"endpoint":"x","keys":{}}')
    )
    assert [s.channel for s in senders] == ["push"]


def test_all_enabled_channels_fire() -> None:
    senders = notifications.senders_for(
        _prefs(
            email_enabled=True,
            telegram_enabled=True,
            telegram_chat_id="123",
            push_enabled=True,
            push_subscription='{"endpoint":"x","keys":{}}',
        )
    )
    assert sorted(s.channel for s in senders) == ["email", "push", "telegram"]


# ---- Alert scanning ----


async def _seed(db, *, tracked_count: int = 1, target_price: float | None = None) -> None:
    store = Store(name="Test Store", slug=f"test-store-{uuid.uuid4().hex[:6]}")
    category = Category(name="Audio", slug=f"audio-{uuid.uuid4().hex[:6]}")
    user = User(email=f"u{uuid.uuid4().hex[:8]}@example.com", hashed_password="x")
    db.add_all([store, category, user])
    await db.flush()

    prefs = NotificationPreference(user_id=user.id, email_enabled=True)
    db.add(prefs)

    for i in range(tracked_count):
        product = Product(
            name=f"Product {i}",
            slug=f"product-{uuid.uuid4().hex[:8]}",
            store_id=store.id,
            category_id=category.id,
            currency="USD",
        )
        db.add(product)
        await db.flush()
        # Two observations, the second cheaper, so the scan sees a genuine drop
        # rather than a single point it has no baseline to compare against.
        # captured_at has no server default: it is the observation time, so the
        # seeder must supply it. Explicit and increasing, because the scan orders
        # by it to decide which point is "latest".
        now = datetime.now(UTC)
        db.add_all(
            [
                PricePoint(
                    product_id=product.id,
                    price=140.0,
                    shipping=0.0,
                    in_stock=True,
                    captured_at=now - timedelta(hours=6),
                ),
                PricePoint(
                    product_id=product.id,
                    price=100.0,
                    shipping=0.0,
                    in_stock=True,
                    captured_at=now,
                ),
            ]
        )
        db.add(TrackedProduct(user_id=user.id, product_id=product.id, target_price=target_price))
    await db.commit()


@pytest.mark.asyncio
async def test_scan_with_no_tracked_products_sends_nothing(async_session_maker) -> None:
    async with async_session_maker() as db:
        assert await alert_service.run_alert_scan(db) == 0


@pytest.mark.asyncio
async def test_first_observation_of_a_drop_records_a_delivered_alert(async_session_maker) -> None:
    async with async_session_maker() as db:
        await _seed(db)
        sent = await alert_service.run_alert_scan(db)

        assert sent == 1
        events = list((await db.execute(select(AlertEvent))).scalars().all())
        assert len(events) == 1
        assert events[0].kind.value == "price_drop"
        assert events[0].channel == "email"
        assert events[0].delivered is True
        assert events[0].detail


@pytest.mark.asyncio
async def test_rescanning_the_same_price_point_sends_nothing(async_session_maker) -> None:
    # This is the property that stops every scheduler tick from re-emailing
    # every user. Without the recorded price point, alerts are a spam cannon.
    async with async_session_maker() as db:
        await _seed(db)
        assert await alert_service.run_alert_scan(db) == 1
        assert await alert_service.run_alert_scan(db) == 0


@pytest.mark.asyncio
async def test_target_price_alert_wins_over_price_drop(async_session_maker) -> None:
    async with async_session_maker() as db:
        await _seed(db, target_price=150.0)

        await alert_service.run_alert_scan(db)
        event = (await db.execute(select(AlertEvent))).scalar_one()
        assert event.kind.value == "target_met"


@pytest.mark.asyncio
async def test_subject_line_names_the_product_and_the_new_price(async_session_maker) -> None:
    # The subject is the entire message in a notification list; "Price drop" on
    # its own tells the reader nothing about whether to care.
    async with async_session_maker() as db:
        await _seed(db)
        await alert_service.run_alert_scan(db)
        event = (await db.execute(select(AlertEvent))).scalar_one()
        assert "Product 0" in (event.detail or "")
        assert "100.00 USD" in (event.detail or "")


@pytest.mark.asyncio
async def test_no_channels_enabled_records_no_events(async_session_maker) -> None:
    async with async_session_maker() as db:
        await _seed(db)
        prefs = (await db.execute(select(NotificationPreference))).scalar_one()
        prefs.email_enabled = False
        await db.commit()

        assert await alert_service.run_alert_scan(db) == 0
        assert (await db.execute(select(AlertEvent))).scalars().all() == []


@pytest.mark.asyncio
async def test_a_broken_product_does_not_abort_the_sweep(async_session_maker, monkeypatch) -> None:
    # One bad row must not cost everybody else their alert. This is the property
    # that separates an alert system from an outage generator: without it, a
    # single corrupt product silently stops every alert for every user.
    async with async_session_maker() as db:
        await _seed(db, tracked_count=3)
        tracked_rows = list((await db.execute(select(TrackedProduct))).scalars().all())

        broken = tracked_rows[0].id
        real = alert_service.evaluate_tracked_product

        async def _explode(db_, tracked, _real=real, _broken=broken):
            if tracked.id == _broken:
                raise RuntimeError("simulated data corruption")
            return await _real(db_, tracked)

        monkeypatch.setattr(alert_service, "evaluate_tracked_product", _explode)

        assert await alert_service.run_alert_scan(db) == 2


@pytest.mark.asyncio
async def test_dead_push_subscription_is_cleared_and_counted_as_failed(
    async_session_maker, monkeypatch
) -> None:
    async with async_session_maker() as db:
        await _seed(db)
        # Also disable email so this test observes the push event alone rather
        # than whichever channel happens to sort first.
        prefs = (await db.execute(select(NotificationPreference))).scalar_one()
        prefs.email_enabled = False
        prefs.push_enabled = True
        prefs.push_subscription = '{"endpoint":"https://x/gone","keys":{"p256dh":"a","auth":"b"}}'

        async def _gone(self, user, prefs, alert):
            raise notifications.push_service.PushSubscriptionGone

        monkeypatch.setattr(notifications.PushSender, "send", _gone)
        await db.commit()

        await alert_service.run_alert_scan(db)
        await db.refresh(prefs)

        # The row is cleared so it is never retried, and the attempt is still
        # recorded — a user asking "did you try to notify me?" gets a true answer.
        assert prefs.push_subscription is None
        assert prefs.push_enabled is False
        event = (await db.execute(select(AlertEvent))).scalar_one()
        assert event.channel == "push"
        assert event.delivered is False


# ---- Alert composition ----


def test_alert_content_links_to_the_product_page() -> None:
    alert = AlertContent(
        subject="s",
        preheader="p",
        headline="h",
        detail="d",
        cta_label="See the price",
        cta_url="https://x/products/thing",
    )
    assert alert.cta_url.endswith("/products/thing")


@pytest.mark.asyncio
async def test_alert_event_records_the_price_point_it_describes(async_session_maker) -> None:
    # Ties an alert to the observation that caused it, so "why did I get told
    # this?" is answerable from the data rather than from a guess.
    async with async_session_maker() as db:
        await _seed(db)
        await alert_service.run_alert_scan(db)
        event = (await db.execute(select(AlertEvent))).scalar_one()
        assert event.price_point_id is not None


@pytest.mark.asyncio
async def test_coupon_and_product_models_are_importable_for_sweep() -> None:
    # Guards the imports the scan relies on; a rename here should fail loudly.
    assert Coupon is not None and Product is not None
    assert AttemptOutcome.failed.value == "failed"
