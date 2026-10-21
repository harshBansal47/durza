"""Privacy: the IP hash salt, the retention scrub, and server-side consent.

Three things are pinned here, because each of them is a promise the privacy
page makes:

* the hash of an IP address is salted, and the salt can be chosen
  independently of the secret key;
* a community vote keeps its verdict but loses the identifier that rate-limits
  it once the retention window has passed;
* an analytics event is only stored when the request carries a consent cookie
  the server signed — the client-side flag is not evidence of anything, and a
  request without it is accepted and dropped.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core import request_meta
from app.core.config import get_settings
from app.core.consent import (
    has_valid_consent,
    issue_consent_cookie,
    parent_domain,
)
from app.models.coupon_verification import CouponVerification

# ------------------------------------------------------------------- the salt


def test_hash_ip_uses_the_secret_key_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(request_meta.settings, "ip_hash_salt", None, raising=False)
    monkeypatch.setattr(request_meta.settings, "secret_key", "the-secret", raising=False)
    assert request_meta.hash_ip("1.2.3.4") == request_meta.hash_ip("1.2.3.4")
    assert "1.2.3.4" not in request_meta.hash_ip("1.2.3.4")


def test_hash_ip_uses_a_dedicated_salt_when_one_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(request_meta.settings, "ip_hash_salt", "own-salt", raising=False)
    monkeypatch.setattr(request_meta.settings, "secret_key", "the-secret", raising=False)
    with_salt = request_meta.hash_ip("1.2.3.4")

    monkeypatch.setattr(request_meta.settings, "ip_hash_salt", None, raising=False)
    assert with_salt != request_meta.hash_ip("1.2.3.4")


# ------------------------------------------------------------ the consent cookie


def test_the_consent_cookie_round_trips() -> None:
    cookie = issue_consent_cookie(
        secret="s3cret", max_age_days=365, site_url="https://www.durza.in"
    )
    assert has_valid_consent(cookie.value, secret="s3cret", max_age_days=365)


def test_a_tampered_cookie_is_not_consent() -> None:
    cookie = issue_consent_cookie(
        secret="s3cret", max_age_days=365, site_url="https://www.durza.in"
    )
    assert not has_valid_consent(cookie.value + "x", secret="s3cret", max_age_days=365)
    assert not has_valid_consent(cookie.value, secret="other", max_age_days=365)
    assert not has_valid_consent("accepted.1.deadbeef", secret="s3cret", max_age_days=365)
    assert not has_valid_consent(None, secret="s3cret", max_age_days=365)


def test_an_expired_cookie_is_not_consent() -> None:
    issued = int((datetime.now(UTC) - timedelta(days=400)).timestamp())
    cookie = issue_consent_cookie(
        secret="s3cret", max_age_days=365, site_url="https://www.durza.in", issued_at=issued
    )
    assert not has_valid_consent(cookie.value, secret="s3cret", max_age_days=365)


def test_the_cookie_is_scoped_to_the_parent_domain() -> None:
    assert parent_domain("https://www.durza.in") == ".durza.in"
    assert parent_domain("https://api.durza.in") == ".durza.in"
    # A registrable domain has no parent to share with, and localhost has none.
    assert parent_domain("https://durza.in") is None
    assert parent_domain("http://localhost:3000") is None


# ------------------------------------------------------- the endpoints


async def test_an_event_without_consent_is_accepted_and_dropped(
    client, async_session_maker
) -> None:
    from sqlalchemy import func, select

    from app.models.analytics_event import AnalyticsEvent

    resp = await client.post(
        "/api/v1/analytics/event",
        json={"event": "page_view", "page_type": "homepage"},
        headers={"x-session-id": str(uuid.uuid4())},
    )
    assert resp.status_code == 202

    async with async_session_maker() as db:
        count = (await db.execute(select(func.count()).select_from(AnalyticsEvent))).scalar_one()
    assert count == 0


async def test_an_event_with_consent_is_stored(client, async_session_maker) -> None:
    from sqlalchemy import func, select

    from app.models.analytics_event import AnalyticsEvent

    cookie = issue_consent_cookie(
        secret=get_settings().secret_key,
        max_age_days=365,
        site_url=get_settings().site_url,
    )
    resp = await client.post(
        "/api/v1/analytics/event",
        json={"event": "page_view", "page_type": "homepage"},
        headers={"x-session-id": str(uuid.uuid4()), "cookie": f"cz_consent={cookie.value}"},
    )
    assert resp.status_code == 202

    async with async_session_maker() as db:
        count = (await db.execute(select(func.count()).select_from(AnalyticsEvent))).scalar_one()
    assert count == 1


async def test_the_consent_endpoint_issues_a_signed_cookie(client) -> None:
    resp = await client.post("/api/v1/analytics/consent", json={"choice": "accepted"})
    assert resp.status_code == 204
    headers = resp.headers
    assert "cz_consent=" in headers.get("set-cookie", "")
    assert "HttpOnly" in headers.get("set-cookie", "")


async def test_the_consent_endpoint_clears_the_cookie_on_decline(client) -> None:
    resp = await client.post("/api/v1/analytics/consent", json={"choice": "declined"})
    assert resp.status_code == 204
    cookie_header = resp.headers.get("set-cookie", "")
    assert "cz_consent=" in cookie_header
    assert "Max-Age=0" in cookie_header or "expires=" in cookie_header.lower()


# -------------------------------------------------------- the retention scrub


async def test_the_scrub_forgets_old_verification_hashes(async_session_maker) -> None:
    from app.models.category import Category
    from app.models.coupon import Coupon, DiscountType
    from app.models.store import Store
    from app.services import coupon_service

    async with async_session_maker() as db:
        store = Store(name="Store", slug=f"store-{uuid.uuid4().hex[:6]}")
        category = Category(name="Cat", slug=f"cat-{uuid.uuid4().hex[:6]}")
        db.add_all([store, category])
        await db.flush()
        coupon = Coupon(
            title="20% off",
            slug=f"deal-{uuid.uuid4().hex[:8]}",
            code="SAVE20",
            discount_type=DiscountType.percentage,
            discount_value=20.0,
            store_id=store.id,
            category_id=category.id,
            destination_url="https://example.com/deal",
        )
        db.add(coupon)
        await db.flush()

        old = CouponVerification(
            coupon_id=coupon.id,
            ip_hash="a" * 64,
            worked=True,
            created_at=datetime.now(UTC) - timedelta(days=120),
        )
        new = CouponVerification(
            coupon_id=coupon.id,
            ip_hash="b" * 64,
            worked=True,
            created_at=datetime.now(UTC) - timedelta(days=2),
        )
        db.add_all([old, new])
        await db.commit()
        old_id, new_id = old.id, new.id

    async with async_session_maker() as db:
        scrubbed = await coupon_service.scrub_verification_hashes(db, older_than_days=90)
        assert scrubbed == 1
        scrubbed_row = await db.get(CouponVerification, old_id)
        kept_row = await db.get(CouponVerification, new_id)
        assert scrubbed_row is not None
        assert scrubbed_row.ip_hash is None
        assert scrubbed_row.worked is True
        assert kept_row is not None
        assert kept_row.ip_hash == "b" * 64
