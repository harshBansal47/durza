"""Batched impression ingestion and the consent-time session.

Baseline (measured before the change, one request per card): five cookie-less
impressions produced five rows and five AnalyticsSession rows.
"""

import uuid

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.consent import issue_consent_cookie
from app.models.analytics_session import AnalyticsSession
from app.models.coupon_impression import CouponImpression
from tests.test_query_budget import _seed_store_with_offers, count_queries

API = "/api/v1"


def _consent_cookies() -> dict[str, str]:
    settings = get_settings()
    cookie = issue_consent_cookie(
        secret=settings.secret_key,
        max_age_days=settings.analytics_consent_max_age_days,
        site_url=settings.site_url,
    )
    return {settings.analytics_consent_cookie: cookie.value}


def _item(coupon, store, position):
    return {
        "coupon_id": coupon["id"],
        "merchant_id": store["id"],
        "page_type": "deals",
        "position": position,
    }


async def _counts(async_session_maker):
    async with async_session_maker() as db:
        return (
            await db.scalar(select(func.count()).select_from(CouponImpression)),
            await db.scalar(select(func.count()).select_from(AnalyticsSession)),
        )


async def _setup(client, admin_headers, offers=5):
    store, _ = await _seed_store_with_offers(client, admin_headers, offers=offers, articles=0)
    coupons = (await client.get(f"{API}/coupons")).json()["items"]
    client.cookies.clear()
    return store, coupons


async def test_batch_stores_every_impression_in_one_session_and_one_commit(
    client, admin_headers, async_session_maker, engine
):
    store, coupons = await _setup(client, admin_headers)
    body = {"impressions": [_item(c, store, i + 1) for i, c in enumerate(coupons)]}

    async with count_queries(engine) as statements:
        resp = await client.post(
            f"{API}/analytics/impressions", json=body, cookies=_consent_cookies()
        )
    assert resp.status_code == 202
    rows, sessions = await _counts(async_session_maker)
    assert (rows, sessions) == (5, 1)
    inserts = [
        s for s in statements if s.lstrip().upper().startswith("INSERT INTO COUPON_IMPRESSIONS")
    ]
    print(
        f"\n[batch] 5 impressions: {len(statements)} statements total, {len(inserts)} impression insert(s)"
    )
    # Before: 5 requests. Now: one request, rows written together.
    assert len(statements) <= 6, statements


async def test_batch_without_consent_stores_nothing(client, admin_headers, async_session_maker):
    store, coupons = await _setup(client, admin_headers)
    resp = await client.post(
        f"{API}/analytics/impressions",
        json={"impressions": [_item(coupons[0], store, 1)]},
    )
    assert resp.status_code == 202
    assert await _counts(async_session_maker) == (0, 0)


async def test_batch_rejects_empty_and_oversized(client, admin_headers):
    store, coupons = await _setup(client, admin_headers, offers=1)
    cookies = _consent_cookies()
    empty = await client.post(
        f"{API}/analytics/impressions", json={"impressions": []}, cookies=cookies
    )
    assert empty.status_code == 422
    too_many = {"impressions": [_item(coupons[0], store, 1)] * 51}
    assert (
        await client.post(f"{API}/analytics/impressions", json=too_many, cookies=cookies)
    ).status_code == 422


async def test_batch_validates_each_item_like_the_single_endpoint(client, admin_headers):
    store, coupons = await _setup(client, admin_headers, offers=1)
    bad = _item(coupons[0], store, 101)  # position is capped at 100
    resp = await client.post(
        f"{API}/analytics/impressions", json={"impressions": [bad]}, cookies=_consent_cookies()
    )
    assert resp.status_code == 422


async def test_batch_drops_only_a_row_that_references_a_missing_coupon(
    client, admin_headers, async_session_maker
):
    store, coupons = await _setup(client, admin_headers, offers=2)
    ghost = {**_item(coupons[0], store, 2), "coupon_id": str(uuid.uuid4())}
    body = {"impressions": [_item(coupons[0], store, 1), ghost, _item(coupons[1], store, 3)]}
    resp = await client.post(f"{API}/analytics/impressions", json=body, cookies=_consent_cookies())
    assert resp.status_code == 202
    rows, _ = await _counts(async_session_maker)
    assert rows == 2


async def test_single_impression_endpoint_still_works(client, admin_headers, async_session_maker):
    store, coupons = await _setup(client, admin_headers, offers=1)
    resp = await client.post(
        f"{API}/analytics/impression", json=_item(coupons[0], store, 1), cookies=_consent_cookies()
    )
    assert resp.status_code == 202
    assert (await _counts(async_session_maker))[0] == 1


async def test_accepting_consent_issues_the_session_cookie_once(client, async_session_maker):
    first = await client.post(f"{API}/analytics/consent", json={"choice": "accepted"})
    assert first.status_code == 204
    assert "cz_sid" in first.headers.get("set-cookie", "")
    _, sessions = await _counts(async_session_maker)
    assert sessions == 1

    # Re-syncing consent with the cookie already present must not mint another one.
    sid = next(c.value for c in client.cookies.jar if c.name == "cz_sid")
    again = await client.post(
        f"{API}/analytics/consent", json={"choice": "accepted"}, cookies={"cz_sid": sid}
    )
    assert again.status_code == 204
    assert (await _counts(async_session_maker))[1] == 1


async def test_declining_consent_creates_no_session(client, async_session_maker):
    resp = await client.post(f"{API}/analytics/consent", json={"choice": "declined"})
    assert resp.status_code == 204
    assert (await _counts(async_session_maker))[1] == 0
