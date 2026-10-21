"""POST /api/v1/analytics/event: shopper interactions reported by the storefront.

The storefront's `track()` sends a flat JSON object. These tests send exactly that
shape, so a change on either side that would silently lose events fails here.
"""

import uuid

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.consent import issue_consent_cookie
from app.models.analytics_event import AnalyticsEvent
from app.routers.api.v1.analytics import sanitize_props

URL = "/api/v1/analytics/event"


@pytest.fixture(autouse=True)
def _consented(client):
    """These tests are about the shape of an event, not about consent.

    The server only records an event when the request carries a consent cookie
    it signed; without one the event is accepted and dropped. That gate has
    its own tests (tests/test_privacy_consent.py), so here every request
    carries the cookie.
    """
    cookie = issue_consent_cookie(
        secret=get_settings().secret_key,
        max_age_days=get_settings().analytics_consent_max_age_days,
        site_url=get_settings().site_url,
    )
    client.cookies.set(get_settings().analytics_consent_cookie, cookie.value)


async def _events(async_session_maker):
    async with async_session_maker() as db:
        return list((await db.execute(select(AnalyticsEvent))).scalars())


async def test_records_a_copy_event_in_the_storefronts_flat_shape(client, async_session_maker):
    coupon_id = str(uuid.uuid4())
    resp = await client.post(
        URL, json={"event": "copy_code", "coupon_id": coupon_id, "ts": "2026-10-06T10:00:00.000Z"}
    )
    assert resp.status_code == 202
    (event,) = await _events(async_session_maker)
    assert event.name == "copy_code"
    assert str(event.coupon_id) == coupon_id
    assert event.props is None  # `ts` is not a property


async def test_search_query_is_kept_as_a_property(client, async_session_maker):
    resp = await client.post(URL, json={"event": "search", "query": "nike shoes", "results": 12})
    assert resp.status_code == 202
    (event,) = await _events(async_session_maker)
    assert event.props == {"query": "nike shoes", "results": 12}


async def test_visitor_and_session_come_from_the_cookie_and_header(client, async_session_maker):
    client.cookies.set("cz_vid", "visitor-abc")
    await client.post(URL, json={"event": "page_view"}, headers={"x-session-id": "sess-1"})
    (event,) = await _events(async_session_maker)
    assert event.visitor_id == "visitor-abc"
    assert event.session_id == "sess-1"


async def test_merchant_id_is_stored_as_the_store(client, async_session_maker):
    store_id = str(uuid.uuid4())
    await client.post(URL, json={"event": "follow_store", "merchant_id": store_id})
    (event,) = await _events(async_session_maker)
    assert str(event.store_id) == store_id


@pytest.mark.parametrize(
    "name",
    [
        "page_view",
        "search",
        "coupon_click",
        "coupon_reveal",
        "copy_code",
        "save_coupon",
        "unsave_coupon",
        "follow_store",
        "unfollow_store",
        "merchant_click",
        "filter_change",
        "verify_vote",
    ],
)
async def test_every_event_the_storefront_can_send_is_accepted(client, name):
    assert (await client.post(URL, json={"event": name})).status_code == 202


@pytest.mark.parametrize("name", ["coupon_impression", "purchase", "", "COPY_CODE", "drop_table"])
async def test_unknown_event_names_are_rejected(client, async_session_maker, name):
    resp = await client.post(URL, json={"event": name})
    assert resp.status_code == 422
    assert await _events(async_session_maker) == []


async def test_missing_event_name_is_rejected(client):
    assert (await client.post(URL, json={"coupon_id": str(uuid.uuid4())})).status_code == 422


async def test_a_malformed_id_costs_one_field_not_the_event(client, async_session_maker):
    resp = await client.post(URL, json={"event": "copy_code", "coupon_id": "not-a-uuid"})
    assert resp.status_code == 202
    (event,) = await _events(async_session_maker)
    assert event.name == "copy_code" and event.coupon_id is None


async def test_an_event_for_an_unknown_coupon_still_records(client, async_session_maker):
    await client.post(URL, json={"event": "coupon_click", "coupon_id": str(uuid.uuid4())})
    assert len(await _events(async_session_maker)) == 1  # no foreign key to violate


async def test_overlong_values_are_truncated_and_junk_is_dropped(client, async_session_maker):
    await client.post(
        URL,
        json={
            "event": "search",
            "query": "x" * 5000,
            "nested": {"a": 1},
            "list": [1, 2],
            "Bad-Key": "no",
            "ok_flag": True,
        },
    )
    (event,) = await _events(async_session_maker)
    assert event.props == {"query": "x" * 200, "ok_flag": True}


def test_sanitize_props_caps_the_number_of_properties():
    props = sanitize_props({f"k{i}": i for i in range(50)})
    assert props is not None and len(props) == 10


def test_sanitize_props_returns_none_when_nothing_is_usable():
    assert sanitize_props(None) is None
    assert sanitize_props({"ts": "now", "event": "x", "nested": {"a": 1}}) is None


async def test_preflight_allows_credentialed_cors_and_session_header(client):
    resp = await client.options(
        URL,
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-session-id",
        },
    )
    assert resp.status_code in (200, 204)
    assert resp.headers.get("access-control-allow-credentials") == "true"
    assert "http://localhost:3000" in (resp.headers.get("access-control-allow-origin") or "")
    allowed = (resp.headers.get("access-control-allow-headers") or "").lower()
    assert "x-session-id" in allowed
