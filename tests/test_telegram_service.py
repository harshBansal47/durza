"""Posting new offers to the Telegram channel: once, in moderation, and honestly."""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.core import observability
from app.core.config import get_settings
from app.integrations import telegram as tg_api
from app.integrations.telegram import (
    TelegramClient,
    TelegramConfigError,
    TelegramRateLimited,
    TelegramRejected,
    TelegramTemporary,
    TelegramUncertain,
)
from app.models.category import Category
from app.models.coupon import Coupon, CouponStatus, DiscountType, VerificationStatus
from app.models.store import Store
from app.models.telegram_post import TelegramPost
from app.services import telegram_service as svc

pytestmark = pytest.mark.asyncio

TOKEN = "123456789:AAH" + "x" * 32


class FakeTelegram:
    """Records what would have been posted. `outcomes` scripts the first few calls."""

    def __init__(self, *outcomes: Exception | None) -> None:
        self.outcomes = list(outcomes)
        self.sent: list[SimpleNamespace] = []

    async def send_message(self, text, *, button=None, silent=False) -> int:
        self.sent.append(SimpleNamespace(text=text, button=button, silent=silent))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome
        return 1000 + len(self.sent)


class Naps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def cfg(monkeypatch):
    settings = get_settings()
    for name, value in {
        "telegram_channel_enabled": True,
        "telegram_bot_token": TOKEN,
        "telegram_channel_id": "@durzatest",
        "site_url": "https://durza.in",
        "telegram_send_gap_seconds": 0.0,
        "telegram_country": "IN",
        "telegram_only_verified": False,
        "telegram_max_posts_per_day": 40,
        "telegram_min_hours_to_expiry": 3,
    }.items():
        monkeypatch.setattr(settings, name, value)
    return settings


async def _world(db):
    tag = uuid.uuid4().hex[:6]
    store = Store(name="Nykaa", slug=f"nykaa-{tag}")
    cat = Category(name=f"Beauty-{tag}", slug=f"beauty-{tag}")
    db.add_all([store, cat])
    await db.commit()
    return store, cat


async def _coupon(db, store, cat, **kw) -> Coupon:
    fields = {
        "title": "Flat 20% off on skincare",
        "slug": f"c-{uuid.uuid4().hex[:10]}",
        "code": f"NYK{uuid.uuid4().hex[:5].upper()}",
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


async def _ledger(db) -> list[TelegramPost]:
    return list((await db.execute(select(TelegramPost))).scalars().all())


def _transient(**kw) -> tuple[Coupon, Store]:
    store = Store(name="Myntra", slug="myntra", currency="INR")
    store.id = uuid.uuid4()
    fields = {
        "id": uuid.uuid4(),
        "title": "Flat 20% off on fashion",
        "slug": "myntra-20",
        "code": "MYN20",
        "discount_type": DiscountType.percentage,
        "discount_value": 20,
        "store_id": store.id,
        "destination_url": "https://example.com",
    } | kw
    return Coupon(**fields), store


# --- wording ------------------------------------------------------------------


async def test_post_has_the_essentials_and_escapes_markup(cfg):
    coupon, store = _transient(title="Buy <b>1</b> get 1 & more")
    now = datetime.now(UTC)
    post = svc.prepare(coupon, store, now=now, settings=cfg)

    assert "<b>Myntra</b>" in post.text
    assert "<code>MYN20</code>" in post.text
    assert "Buy &lt;b&gt;1&lt;/b&gt; get 1 &amp; more" in post.text
    assert cfg.telegram_footer in post.text
    _label, url = post.button
    assert (
        url
        == "https://durza.in/coupons/myntra-20?utm_source=telegram&utm_medium=channel&utm_campaign=new_offers"
    )


async def test_a_non_https_site_url_degrades_to_a_text_link_not_a_rejected_message(
    cfg, monkeypatch
):
    monkeypatch.setattr(cfg, "site_url", "http://localhost:3000")
    coupon, store = _transient()
    post = svc.prepare(coupon, store, now=datetime.now(UTC), settings=cfg)
    assert post.button is None
    assert "localhost:3000/coupons/myntra-20" in post.text


async def test_an_offer_without_a_code_says_no_code_needed(cfg):
    coupon, store = _transient()
    coupon.code = None
    assert "no code needed" in svc.prepare(coupon, store, now=datetime.now(UTC), settings=cfg).text


async def test_verified_is_claimed_only_when_it_is_true_and_recent(cfg):
    now = datetime.now(UTC)
    coupon, store = _transient(
        verification_status=VerificationStatus.verified, last_verified_at=now - timedelta(hours=5)
    )
    assert "Verified 5h ago" in svc.prepare(coupon, store, now=now, settings=cfg).text

    coupon.last_verified_at = now - timedelta(days=10)
    assert "Verified" not in svc.prepare(coupon, store, now=now, settings=cfg).text

    coupon.verification_status = VerificationStatus.unverified
    coupon.last_verified_at = now - timedelta(hours=1)
    assert "Verified" not in svc.prepare(coupon, store, now=now, settings=cfg).text


async def test_the_discount_is_not_repeated_when_the_title_already_says_it(cfg):
    coupon, store = _transient()
    text = svc.prepare(coupon, store, now=datetime.now(UTC), settings=cfg).text
    assert text.count("20%") == 1

    coupon.title = "Fashion sale"
    assert (
        "20% OFF — Fashion sale"
        in svc.prepare(coupon, store, now=datetime.now(UTC), settings=cfg).text
    )


async def test_amounts_use_indian_grouping():
    assert [svc._inr(n) for n in (200, 1999, 100000, 12345678)] == [
        "200",
        "1,999",
        "1,00,000",
        "1,23,45,678",
    ]


async def test_validity_is_in_indian_time_and_a_placeholder_date_is_left_out(cfg):
    now = datetime(2026, 11, 1, 6, 0, tzinfo=UTC)  # 11:30 IST
    assert svc._valid_till(datetime(2026, 11, 1, 18, 29, tzinfo=UTC), now) == "today, 11:59 PM"
    assert svc._valid_till(datetime(2026, 11, 5, 18, 29, tzinfo=UTC), now) == "5 Nov"
    assert svc._valid_till(datetime(2099, 1, 1, tzinfo=UTC), now) is None


async def test_posts_are_silent_overnight_in_india_only():
    assert svc._is_night(datetime(2026, 11, 1, 20, 30, tzinfo=UTC))  # 02:00 IST
    assert not svc._is_night(datetime(2026, 11, 1, 6, 30, tzinfo=UTC))  # 12:00 IST


# --- identity -----------------------------------------------------------------


async def test_the_same_code_and_discount_is_the_same_offer_whatever_the_title():
    a, _ = _transient(normalized_code="MYN20")
    b, _ = _transient(normalized_code="MYN20")
    b.store_id, b.title = a.store_id, "Completely different wording"
    assert svc.dedupe_key(a) == svc.dedupe_key(b)

    b.discount_value = 30  # same code, new terms: news again
    assert svc.dedupe_key(a) != svc.dedupe_key(b)

    b.discount_value, b.store_id = 20, uuid.uuid4()  # same code at another store
    assert svc.dedupe_key(a) != svc.dedupe_key(b)


async def test_a_deal_without_a_code_is_identified_by_its_wording():
    a, _ = _transient()
    b, _ = _transient()
    a.code = b.code = None
    b.store_id, b.title = a.store_id, "  FLAT 20%   off on Fashion "
    assert svc.dedupe_key(a) == svc.dedupe_key(b)


# --- the run ------------------------------------------------------------------


async def test_an_offer_is_posted_once_however_often_the_scan_runs(async_session_maker, cfg):
    fake, naps = FakeTelegram(), Naps()
    async with async_session_maker() as db:
        store, cat = await _world(db)
        await _coupon(db, store, cat)

        assert (
            await svc.post_new_offers(db, client=fake, sleep=naps)
            == "posted 1, skipped 0 duplicate(s)"
        )
        assert await svc.post_new_offers(db, client=fake, sleep=naps) == "nothing new"
        assert await svc.post_new_offers(db, client=fake, sleep=naps) == "nothing new"

        assert len(fake.sent) == 1
        (row,) = await _ledger(db)
        assert (row.status, row.message_id, row.store_name) == ("sent", 1001, "Nykaa")
        assert row.sent_at is not None


async def test_the_same_offer_arriving_as_a_new_coupon_row_is_not_posted_again(
    async_session_maker, cfg
):
    fake = FakeTelegram()
    async with async_session_maker() as db:
        store, cat = await _world(db)
        first = await _coupon(db, store, cat, code="SAVE20", normalized_code="SAVE20")
        await svc.post_new_offers(db, client=fake, sleep=Naps())
        assert len(fake.sent) == 1

        # A re-import creates a fresh row for the same offer, under a new slug and title.
        await _coupon(
            db, store, cat, code="SAVE20", normalized_code="SAVE20", title="Save 20% today"
        )
        assert (
            await svc.post_new_offers(db, client=fake, sleep=Naps())
            == "posted 0, skipped 1 duplicate(s)"
        )
        assert await svc.post_new_offers(db, client=fake, sleep=Naps()) == "nothing new"

        assert len(fake.sent) == 1
        assert sorted(r.status for r in await _ledger(db)) == ["duplicate", "sent"]
        assert first.id is not None


async def test_two_copies_in_one_scan_post_one(async_session_maker, cfg):
    fake = FakeTelegram()
    async with async_session_maker() as db:
        store, cat = await _world(db)
        await _coupon(db, store, cat, code="TWIN", normalized_code="TWIN")
        await _coupon(db, store, cat, code="TWIN", normalized_code="TWIN", title="Twin offer")
        assert (
            await svc.post_new_offers(db, client=fake, sleep=Naps())
            == "posted 1, skipped 1 duplicate(s)"
        )
        assert len(fake.sent) == 1


async def test_old_live_unsent_offers_are_announced_but_ineligible_offers_are_not(
    async_session_maker, cfg
):
    fake = FakeTelegram()
    now = datetime.now(UTC)
    async with async_session_maker() as db:
        store, cat = await _world(db)
        dead_store = Store(name="Gone", slug=f"gone-{uuid.uuid4().hex[:6]}", is_active=False)
        db.add(dead_store)
        await db.commit()
        await _coupon(db, store, cat, created_at=now - timedelta(days=3))  # still eligible
        await _coupon(db, store, cat, expires_at=now - timedelta(hours=1))  # expired
        await _coupon(db, store, cat, expires_at=now + timedelta(hours=1))  # about to lapse
        await _coupon(db, store, cat, status=CouponStatus.pending_validation)
        await _coupon(db, store, cat, is_active=False)
        await _coupon(db, store, cat, country="US")
        await _coupon(db, store, cat, starts_at=now + timedelta(days=1))
        await _coupon(db, store, cat, verification_status=VerificationStatus.failed)
        await _coupon(db, dead_store, cat)

        assert (await svc.post_new_offers(db, client=fake, sleep=Naps())).startswith("posted 1")
        assert len(fake.sent) == 1

        # A newly created live offer is still announced as before.
        await _coupon(db, store, cat)
        assert (await svc.post_new_offers(db, client=fake, sleep=Naps())).startswith("posted 1")


async def test_only_verified_mode_skips_the_rest(async_session_maker, cfg, monkeypatch):
    monkeypatch.setattr(cfg, "telegram_only_verified", True)
    fake = FakeTelegram()
    async with async_session_maker() as db:
        store, cat = await _world(db)
        await _coupon(db, store, cat)
        assert await svc.post_new_offers(db, client=fake, sleep=Naps()) == "nothing new"
        await _coupon(db, store, cat, verification_status=VerificationStatus.verified)
        assert (await svc.post_new_offers(db, client=fake, sleep=Naps())).startswith("posted 1")


async def test_a_big_import_uses_daily_budget_best_offers_and_send_spacing(
    async_session_maker, cfg, monkeypatch
):
    monkeypatch.setattr(cfg, "telegram_max_posts_per_day", 2)
    monkeypatch.setattr(cfg, "telegram_send_gap_seconds", 4.0)
    fake, naps = FakeTelegram(), Naps()
    async with async_session_maker() as db:
        store, cat = await _world(db)
        for score in (10, 90, 50, 80, 20):
            await _coupon(db, store, cat, title=f"Offer scoring {score}", quality_score=score)

        assert (await svc.post_new_offers(db, client=fake, sleep=naps)).startswith("posted 2")
        assert [("90" in m.text, "80" in m.text) for m in fake.sent] == [
            (True, False),
            (False, True),
        ]
        assert naps.calls == [4.0]  # one pause between two posts, none after the last


async def test_the_daily_cap_holds_across_scans(async_session_maker, cfg, monkeypatch):
    monkeypatch.setattr(cfg, "telegram_max_posts_per_day", 3)
    fake = FakeTelegram()
    async with async_session_maker() as db:
        store, cat = await _world(db)
        for i in range(6):
            await _coupon(db, store, cat, title=f"Distinct offer {i}")
        assert (await svc.post_new_offers(db, client=fake, sleep=Naps())).startswith("posted 3")
        assert await svc.post_new_offers(db, client=fake, sleep=Naps()) == "daily cap reached"
        assert len(fake.sent) == 3


# --- failure handling ---------------------------------------------------------


async def test_a_config_error_stops_the_run_and_the_offer_is_tried_again_once_fixed(
    async_session_maker, cfg
):
    async with async_session_maker() as db:
        store, cat = await _world(db)
        await _coupon(db, store, cat)

        broken = FakeTelegram(TelegramConfigError("Forbidden: bot was kicked", code=403))
        assert "stopped: telegram config error" in await svc.post_new_offers(
            db, client=broken, sleep=Naps()
        )
        assert await _ledger(db) == []  # the claim was released, not burnt

        fixed = FakeTelegram()
        assert (await svc.post_new_offers(db, client=fixed, sleep=Naps())).startswith("posted 1")


async def test_telegram_being_down_releases_the_claim(async_session_maker, cfg):
    async with async_session_maker() as db:
        store, cat = await _world(db)
        await _coupon(db, store, cat)
        down = FakeTelegram(TelegramTemporary("Bad Gateway", code=502))
        assert "stopped" in await svc.post_new_offers(db, client=down, sleep=Naps())
        assert await _ledger(db) == []
        assert (await svc.post_new_offers(db, client=FakeTelegram(), sleep=Naps())).startswith(
            "posted 1"
        )


async def test_a_message_telegram_refuses_is_recorded_and_never_retried(async_session_maker, cfg):
    async with async_session_maker() as db:
        store, cat = await _world(db)
        await _coupon(db, store, cat, title="Bad one")
        await _coupon(db, store, cat, title="Good one")
        fake = FakeTelegram(TelegramRejected("can't parse entities", code=400))

        result = await svc.post_new_offers(db, client=fake, sleep=Naps())
        assert result.startswith("posted 1")  # it carried on to the next offer
        assert sorted(r.status for r in await _ledger(db)) == ["rejected", "sent"]

        assert await svc.post_new_offers(db, client=fake, sleep=Naps()) == "nothing new"
        assert len(fake.sent) == 2


async def test_an_unconfirmed_send_is_never_repeated(async_session_maker, cfg):
    async with async_session_maker() as db:
        store, cat = await _world(db)
        await _coupon(db, store, cat)
        fake = FakeTelegram(TelegramUncertain("no answer from Telegram (ReadTimeout)"))

        assert "stopped" in await svc.post_new_offers(db, client=fake, sleep=Naps())
        (row,) = await _ledger(db)
        assert row.status == "uncertain"
        assert await svc.post_new_offers(db, client=fake, sleep=Naps()) == "nothing new"
        assert len(fake.sent) == 1  # it might have been delivered, so it is not sent again


async def test_a_short_rate_limit_is_waited_out_and_a_long_one_stops_the_run(
    async_session_maker, cfg
):
    async with async_session_maker() as db:
        store, cat = await _world(db)
        await _coupon(db, store, cat)
        naps = Naps()
        short = FakeTelegram(TelegramRateLimited("slow down", retry_after=2))
        assert (await svc.post_new_offers(db, client=short, sleep=naps)).startswith("posted 1")
        assert naps.calls == [3] and len(short.sent) == 2

        await _coupon(db, store, cat, title="Another offer")
        long_wait = FakeTelegram(TelegramRateLimited("slow down", retry_after=300))
        assert "stopped" in await svc.post_new_offers(db, client=long_wait, sleep=Naps())
        assert len(long_wait.sent) == 1
        assert [r.status for r in await _ledger(db)] == ["sent"]  # the second claim was released


async def test_a_claim_left_mid_send_by_a_crash_becomes_uncertain_and_is_not_resent(
    async_session_maker, cfg
):
    async with async_session_maker() as db:
        store, cat = await _world(db)
        coupon = await _coupon(db, store, cat)
        db.add(
            TelegramPost(
                coupon_id=coupon.id,
                dedupe_key=svc.dedupe_key(coupon),
                status="pending",
                chat_id="@durzatest",
                store_name="Nykaa",
                title=coupon.title,
                created_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        await db.commit()

        fake = FakeTelegram()
        assert await svc.post_new_offers(db, client=fake, sleep=Naps()) == "nothing new"
        assert fake.sent == []
        (row,) = await _ledger(db)
        assert row.status == "uncertain"


async def test_it_does_nothing_unless_switched_on_and_configured(
    async_session_maker, cfg, monkeypatch
):
    async with async_session_maker() as db:
        store, cat = await _world(db)
        await _coupon(db, store, cat)

        monkeypatch.setattr(cfg, "telegram_channel_enabled", False)
        assert await svc.post_new_offers(db) == "telegram disabled"

        monkeypatch.setattr(cfg, "telegram_channel_enabled", True)
        monkeypatch.setattr(cfg, "telegram_channel_id", None)
        assert await svc.post_new_offers(db) == "telegram not configured"
        assert await _ledger(db) == []


# --- the API client -----------------------------------------------------------


def _response(status: int, body: dict) -> httpx.Response:
    return httpx.Response(status, json=body)


async def test_telegram_answers_are_sorted_by_what_they_imply():
    ok = _response(200, {"ok": True, "result": {"message_id": 7}})
    assert tg_api._raise_for(ok) == {"message_id": 7}

    def error(status, description, **params):
        body = {"ok": False, "error_code": status, "description": description}
        if params:
            body["parameters"] = params
        return _response(status, body)

    with pytest.raises(TelegramRateLimited) as slow:
        tg_api._raise_for(error(429, "Too Many Requests", retry_after=12))
    assert slow.value.retry_after == 12
    for status, text in (
        (401, "Unauthorized"),
        (403, "Forbidden: bot is not a member"),
        (404, "Not Found"),
    ):
        with pytest.raises(TelegramConfigError):
            tg_api._raise_for(error(status, text))
    with pytest.raises(TelegramConfigError):
        tg_api._raise_for(error(400, "Bad Request: chat not found"))
    with pytest.raises(TelegramRejected):
        tg_api._raise_for(error(400, "Bad Request: can't parse entities"))
    with pytest.raises(TelegramTemporary):
        tg_api._raise_for(error(502, "Bad Gateway"))


async def test_a_failed_call_never_exposes_the_bot_token():
    """The token is in the URL, and httpx puts the URL in its error messages."""

    class Down:
        async def post(self, *_a, **_k):
            raise httpx.ReadTimeout(
                "timed out for https://api.telegram.org/bot" + TOKEN + "/sendMessage"
            )

    client = TelegramClient(TOKEN, "@durzatest")
    client._http = Down()  # type: ignore[assignment]
    with pytest.raises(TelegramUncertain) as caught:
        await client.send_message("hi")
    assert TOKEN not in str(caught.value) and "AAH" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


async def test_a_connection_failure_is_safe_to_retry_but_a_read_timeout_is_not():
    class Unreachable:
        async def post(self, *_a, **_k):
            raise httpx.ConnectError("boom")

    client = TelegramClient(TOKEN, "@durzatest")
    client._http = Unreachable()  # type: ignore[assignment]
    with pytest.raises(TelegramTemporary):
        await client.send_message("hi")


# --- the token must not reach a log -------------------------------------------


async def test_a_bot_token_is_redacted_from_stored_log_text():
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    scrubbed = observability.scrub_sensitive(f"Client error for url '{url}' from jane@example.com")
    assert TOKEN not in scrubbed and "AAH" not in scrubbed and "@example.com" not in scrubbed
    assert "[redacted-token]" in scrubbed and "api.telegram.org/bot" in scrubbed


async def test_httpx_is_quietened_so_request_urls_are_not_logged_at_info():
    observability.configure_logging("INFO", "text")
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
