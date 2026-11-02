"""Saved-item email updates, digests, and unsubscribe."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import Base
from app.core.unsubscribe import make_unsubscribe_token, read_unsubscribe_token
from app.models.category import Category
from app.models.coupon import Coupon, CouponStatus, DiscountType, VerificationStatus
from app.models.saved_items import NotificationPreference, SavedCoupon, SavedStore
from app.models.store import Store
from app.models.user import Role
from app.schemas.user import UserCreate
from app.services import deal_alert_service as engine
from app.services import scheduler, user_service
from app.services.email import AlertContent, AlertItem, build_message

pytestmark = pytest.mark.asyncio

DAY = timedelta(days=1)


async def test_product_tracking_and_alert_history_are_not_mapped() -> None:
    assert "tracked_products" not in Base.metadata.tables
    assert "track_alert_states" not in Base.metadata.tables
    assert "deal_alert_marks" not in Base.metadata.tables
    assert "alert_events" not in Base.metadata.tables


class FakeSender:
    channel = "email"

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[AlertContent] = []

    async def send(self, user, prefs, alert) -> bool:
        self.sent.append(alert)
        return self.ok


@pytest.fixture
def sender(monkeypatch):
    fake = FakeSender()
    monkeypatch.setattr(engine.notifications, "senders_for", lambda prefs: [fake])
    return fake


async def _world(db):
    store = Store(name="Nykaa", slug=f"nykaa-{uuid.uuid4().hex[:6]}")
    cat = Category(name=f"Beauty-{uuid.uuid4().hex[:6]}", slug=f"beauty-{uuid.uuid4().hex[:6]}")
    user = await user_service.create_user(
        db,
        UserCreate(email=f"u-{uuid.uuid4().hex[:8]}@example.com", password="supersecret123"),
        role=Role.user,
    )
    db.add_all([store, cat])
    await db.commit()
    return store, cat, user


async def _coupon(db, store, cat, title="20% off sheet masks", **kw) -> Coupon:
    fields = {
        "title": title,
        "slug": f"c-{uuid.uuid4().hex[:10]}",
        "discount_type": DiscountType.percentage,
        "discount_value": 20,
        "store_id": store.id,
        "category_id": cat.id,
        "destination_url": "https://example.com/x",
        "is_active": True,
        "status": CouponStatus.active,
    } | kw
    coupon = Coupon(**fields)
    db.add(coupon)
    await db.commit()
    return coupon


async def _save_store(db, user, store, *, ago=DAY) -> None:
    db.add(SavedStore(user_id=user.id, store_id=store.id, created_at=datetime.now(UTC) - ago))
    await db.commit()


async def _save_coupon(db, user, coupon, *, ago=DAY) -> None:
    db.add(SavedCoupon(user_id=user.id, coupon_id=coupon.id, created_at=datetime.now(UTC) - ago))
    await db.commit()


# ---- saved stores -------------------------------------------------------------


async def test_new_offer_is_derived_from_the_current_scan_window(async_session_maker, sender):
    async with async_session_maker() as db:
        store, cat, user = await _world(db)
        await _save_store(db, user, store)
        await _coupon(db, store, cat)

        first_scan = datetime.now(UTC)
        assert await engine.evaluate_user(db, user.id, first_scan) == 1
        assert len(sender.sent) == 1
        assert sender.sent[0].subject.startswith("New at Nykaa")

        next_scan = first_scan + timedelta(
            minutes=get_settings().alert_scan_interval_minutes, seconds=1
        )
        assert await engine.evaluate_user(db, user.id, next_scan) == 0
        assert len(sender.sent) == 1


async def test_offers_from_before_the_store_was_saved_are_not_news(async_session_maker, sender):
    async with async_session_maker() as db:
        store, cat, user = await _world(db)
        await _coupon(db, store, cat, created_at=datetime.now(UTC) - 3 * DAY)
        await _save_store(db, user, store, ago=timedelta(hours=1))

        assert await engine.evaluate_user(db, user.id) == 0
        assert sender.sent == []


async def test_offers_that_are_not_live_are_not_announced(async_session_maker, sender):
    async with async_session_maker() as db:
        store, cat, user = await _world(db)
        await _save_store(db, user, store)
        await _coupon(db, store, cat, title="Inactive", is_active=False)
        await _coupon(
            db, store, cat, title="Expired", expires_at=datetime.now(UTC) - timedelta(hours=1)
        )

        assert await engine.evaluate_user(db, user.id) == 0


# ---- saved offers -------------------------------------------------------------


async def test_ending_soon_is_announced_when_a_saved_offer_enters_the_window(
    async_session_maker, sender
):
    async with async_session_maker() as db:
        store, cat, user = await _world(db)
        now = datetime.now(UTC)
        warning_hours = get_settings().deal_alert_ending_soon_hours
        coupon = await _coupon(
            db, store, cat, expires_at=now + timedelta(hours=warning_hours) - timedelta(minutes=5)
        )
        await _save_coupon(db, user, coupon)

        assert await engine.evaluate_user(db, user.id, now) == 1
        assert sender.sent[0].subject.startswith("Ending soon")


async def test_an_offer_ending_in_a_week_is_not_ending_soon(async_session_maker, sender):
    async with async_session_maker() as db:
        store, cat, user = await _world(db)
        coupon = await _coupon(db, store, cat, expires_at=datetime.now(UTC) + 7 * DAY)
        await _save_coupon(db, user, coupon)

        assert await engine.evaluate_user(db, user.id) == 0


async def test_ended_is_announced_only_if_it_ended_after_it_was_saved(
    async_session_maker, sender
):
    async with async_session_maker() as db:
        store, cat, user = await _world(db)
        now = datetime.now(UTC)
        ended_after = await _coupon(
            db, store, cat, title="Ended after", expires_at=now - timedelta(minutes=5)
        )
        await _save_coupon(db, user, ended_after, ago=DAY)
        assert await engine.evaluate_user(db, user.id, now) == 1
        assert sender.sent[0].subject.startswith("No longer available")

    async with async_session_maker() as db:
        store, cat, user = await _world(db)
        ended_before = await _coupon(
            db, store, cat, title="Ended before", expires_at=datetime.now(UTC) - 2 * DAY
        )
        await _save_coupon(db, user, ended_before, ago=timedelta(hours=1))
        assert await engine.evaluate_user(db, user.id) == 0


async def test_a_pending_offer_has_not_ended(async_session_maker, sender):
    async with async_session_maker() as db:
        store, cat, user = await _world(db)
        coupon = await _coupon(
            db, store, cat, is_active=False, status=CouponStatus.pending_validation
        )
        await _save_coupon(db, user, coupon)

        assert await engine.evaluate_user(db, user.id) == 0


async def test_confirmed_requires_a_recent_verification_after_saving(
    async_session_maker, sender
):
    async with async_session_maker() as db:
        store, cat, user = await _world(db)
        now = datetime.now(UTC)
        fresh = await _coupon(
            db,
            store,
            cat,
            title="Fresh",
            verification_status=VerificationStatus.verified,
            last_verified_at=now - timedelta(minutes=5),
        )
        await _save_coupon(db, user, fresh, ago=DAY)
        assert await engine.evaluate_user(db, user.id, now) == 1
        assert sender.sent[0].subject.startswith("Confirmed working")

    async with async_session_maker() as db:
        store, cat, user = await _world(db)
        stale = await _coupon(
            db,
            store,
            cat,
            title="Stale",
            verification_status=VerificationStatus.verified,
            last_verified_at=now - 2 * DAY,
        )
        await _save_coupon(db, user, stale, ago=timedelta(hours=1))
        assert await engine.evaluate_user(db, user.id, now) == 0


async def test_one_mention_per_offer_and_the_urgent_one_wins(async_session_maker, sender):
    async with async_session_maker() as db:
        store, cat, user = await _world(db)
        now = datetime.now(UTC)
        warning_hours = get_settings().deal_alert_ending_soon_hours
        coupon = await _coupon(
            db,
            store,
            cat,
            expires_at=now + timedelta(hours=warning_hours) - timedelta(minutes=5),
            verification_status=VerificationStatus.verified,
            last_verified_at=now - timedelta(minutes=5),
        )
        await _save_coupon(db, user, coupon, ago=DAY)

        assert await engine.evaluate_user(db, user.id, now) == 1
        assert sender.sent[0].subject.startswith("Ending soon")


# ---- the digest, the cap, and failure ------------------------------------------


async def test_several_events_become_one_email_without_alert_history(async_session_maker, sender):
    async with async_session_maker() as db:
        store, cat, user = await _world(db)
        await _save_store(db, user, store)
        await _coupon(db, store, cat, title="Offer A")
        await _coupon(db, store, cat, title="Offer B")

        assert await engine.evaluate_user(db, user.id) == 2
        assert len(sender.sent) == 1
        digest = sender.sent[0]
        assert digest.subject == "2 updates on saved offers"
        assert {i.title for i in digest.items} == {"Offer A", "Offer B"}

async def test_new_pending_offer_is_included_in_the_next_digest(async_session_maker, sender):
    async with async_session_maker() as db:
        store, cat, user = await _world(db)
        await _save_store(db, user, store)
        await _coupon(db, store, cat, title="First")
        first_scan = datetime.now(UTC)
        assert await engine.evaluate_user(db, user.id, first_scan) == 1

        next_scan = first_scan + timedelta(
            minutes=get_settings().alert_scan_interval_minutes, seconds=1
        )
        await _coupon(db, store, cat, title="Second", created_at=next_scan - timedelta(seconds=1))
        assert await engine.evaluate_user(db, user.id, next_scan) == 1
        assert len(sender.sent) == 2
        assert sender.sent[1].subject.startswith("New at Nykaa: Second")


async def test_failed_send_is_not_retained_and_can_retry_on_the_next_scan(
    async_session_maker, monkeypatch
):
    failing = FakeSender(ok=False)
    monkeypatch.setattr(engine.notifications, "senders_for", lambda prefs: [failing])
    async with async_session_maker() as db:
        store, cat, user = await _world(db)
        await _save_store(db, user, store)
        await _coupon(db, store, cat)

        assert await engine.evaluate_user(db, user.id) == 0
        assert await engine.evaluate_user(db, user.id) == 0
        assert len(failing.sent) == 2

        failing.ok = True
        assert await engine.evaluate_user(db, user.id) == 1


async def test_nothing_is_sent_when_every_channel_is_off(async_session_maker, monkeypatch):
    monkeypatch.setattr(engine.notifications, "senders_for", lambda prefs: [])
    async with async_session_maker() as db:
        store, cat, user = await _world(db)
        await _save_store(db, user, store)
        await _coupon(db, store, cat)

        assert await engine.evaluate_user(db, user.id) == 0


async def test_scan_covers_every_user_with_saved_stores(async_session_maker, sender):
    async with async_session_maker() as db:
        store, cat, first = await _world(db)
        second = await user_service.create_user(
            db, UserCreate(email="second@example.com", password="supersecret123"), role=Role.user
        )
        await _save_store(db, first, store)
        await _save_store(db, second, store)
        await _coupon(db, store, cat)

        assert await engine.run_deal_alert_scan(db) == 2
        assert len(sender.sent) == 2


# ---- scheduler ------------------------------------------------------------------


def test_scheduler_job_names_are_parsed_forgivingly():
    base = get_settings()
    assert base.model_copy(update={"scheduler_jobs": ""}).scheduler_job_names is None
    parsed = base.model_copy(update={"scheduler_jobs": " send_deal_alerts , EXPIRE_COUPONS,"})
    assert parsed.scheduler_job_names == {"send_deal_alerts", "expire_coupons"}


def test_scheduler_runs_only_the_allowed_jobs(monkeypatch):
    base = get_settings()
    monkeypatch.setattr(
        scheduler,
        "get_settings",
        lambda: base.model_copy(update={"scheduler_jobs": "send_deal_alerts,expire_coupons"}),
    )
    assert {job.id for job in scheduler.build_scheduler().get_jobs()} == {
        "send_deal_alerts",
        "expire_coupons",
    }

    monkeypatch.setattr(
        scheduler, "get_settings", lambda: base.model_copy(update={"scheduler_jobs": ""})
    )
    all_jobs = {job.id for job in scheduler.build_scheduler().get_jobs()}
    assert "send_deal_alerts" in all_jobs
    assert set(scheduler.JOBS) - {"post_telegram"} == all_jobs


# ---- unsubscribe ------------------------------------------------------------------


def test_unsubscribe_token_round_trips_and_rejects_forgeries():
    token = make_unsubscribe_token("a@example.com")
    assert read_unsubscribe_token(token) == "a@example.com"
    assert read_unsubscribe_token("a@example.com") is None
    assert read_unsubscribe_token(token[:-2] + "xx") is None


def test_alert_email_carries_a_one_click_unsubscribe_and_lists_digest_items():
    message = build_message(
        "u@example.com",
        AlertContent(
            subject="2 updates",
            preheader="p",
            headline="2 updates",
            detail="Here is what changed.",
            cta_label="See everything",
            cta_url="https://durza.in/account/saved",
            items=(AlertItem("New offer", "Offer A", "https://durza.in/coupons/a"),),
        ),
    )
    assert "/account/unsubscribe/one-click?token=" in message["List-Unsubscribe"]
    assert message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    body = message.get_body(preferencelist=("plain",)).get_content()
    assert "New offer: Offer A" in body and "/account/unsubscribe?token=" in body
    assert "Offer A" in message.get_body(preferencelist=("html",)).get_content()


async def test_unsubscribe_endpoint_switches_email_off_even_without_stored_prefs(
    client, async_session_maker
):
    async with async_session_maker() as db:
        _store, _cat, user = await _world(db)
        email = user.email

    ok = await client.post(
        "/api/v1/auth/unsubscribe-email", json={"token": make_unsubscribe_token(email)}
    )
    forged = await client.post("/api/v1/auth/unsubscribe-email", json={"token": email})

    assert ok.status_code == 204 and forged.status_code == 400
    async with async_session_maker() as db:
        prefs = await db.scalar(select(NotificationPreference))
        assert prefs is not None and prefs.email_enabled is False
