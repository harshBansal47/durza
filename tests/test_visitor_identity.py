"""Visitor identity, session cookies, traffic classification, device detection.

Pure-function tests plus a few database-backed session tests. The privacy
property is part of the contract: IDs are random, never derived from PII, and
the code must not set a cookie it wasn't asked for.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from starlette.requests import Request
from starlette.responses import Response

from app.models.analytics_session import AnalyticsSession, DeviceType, TrafficSource
from app.services.visitor_identity import (
    SESSION_COOKIE,
    VISITOR_COOKIE,
    classify_traffic_source,
    detect_device_type,
    extract_utm_params,
    generate_session_id,
    generate_visitor_id,
    get_or_create_session,
    get_or_create_session_id,
    get_or_create_visitor_id,
)

pytestmark = pytest.mark.asyncio


def _request(
    *, cookies: dict[str, str] | None = None, query: str = "", headers: dict | None = None
) -> Request:
    raw_headers = []
    for name, value in (headers or {}).items():
        raw_headers.append((name.lower().encode(), value.encode()))
    if cookies:
        # Starlette parses cookies from the `cookie` header, not the scope dict.
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        raw_headers.append((b"cookie", cookie_header.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/coupons",
            "query_string": query.encode(),
            "headers": raw_headers,
        }
    )


# ---- ID generation -----------------------------------------------------------


async def test_generated_ids_are_64_char_random_hex():
    seen = {generate_visitor_id(), generate_visitor_id(), generate_session_id()}

    assert len(seen) == 3  # no accidental constant
    for value in seen:
        assert len(value) == 64
        int(value, 16)  # valid hex


# ---- cookie helpers ----------------------------------------------------------


async def test_get_or_create_visitor_id_returns_existing_cookie():
    existing = "a" * 64
    request = _request(cookies={VISITOR_COOKIE: existing})
    response = Response()

    assert get_or_create_visitor_id(request, response) == existing
    assert not response.headers.get("set-cookie")


async def test_get_or_create_visitor_id_replaces_malformed_cookie():
    request = _request(cookies={VISITOR_COOKIE: "too-short"})
    response = Response()

    visitor_id = get_or_create_visitor_id(request, response)

    assert visitor_id != "too-short"
    assert len(visitor_id) == 64
    assert VISITOR_COOKIE in response.headers["set-cookie"]


async def test_get_or_create_visitor_id_sets_cookie_when_missing():
    request = _request()
    response = Response()

    visitor_id = get_or_create_visitor_id(request, response)

    assert len(visitor_id) == 64
    cookie = response.headers["set-cookie"]
    assert VISITOR_COOKIE in cookie
    assert "HttpOnly" in cookie


async def test_get_or_create_session_id_returns_existing_cookie():
    existing = "b" * 64
    request = _request(cookies={SESSION_COOKIE: existing})
    response = Response()

    assert get_or_create_session_id(request, response) == existing
    assert not response.headers.get("set-cookie")


async def test_get_or_create_session_id_sets_a_session_cookie_when_missing():
    request = _request()
    response = Response()

    session_id = get_or_create_session_id(request, response)

    assert len(session_id) == 64
    assert SESSION_COOKIE in response.headers["set-cookie"]


# ---- traffic classification --------------------------------------------------


@pytest.mark.parametrize(
    "utm_source,utm_medium,referrer,expected",
    [
        # UTM source that is a known search engine.
        ("google.com", None, None, TrafficSource.seo),
        ("Google", None, None, TrafficSource.seo),
        ("bing.com", None, None, TrafficSource.seo),
        # UTM source that is a known social platform.
        ("facebook.com", None, None, TrafficSource.social),
        ("x.com", None, None, TrafficSource.social),
        # Explicit email source names.
        ("email", None, None, TrafficSource.email),
        ("newsletter", None, None, TrafficSource.email),
        ("mail", None, None, TrafficSource.email),
        # Paid medium.
        ("some-newsletter-site", "cpc", None, TrafficSource.paid),
        ("partner", "PPC", None, TrafficSource.paid),
        # Bare search-engine names in utm_source.
        ("google", None, None, TrafficSource.seo),
        ("duckduckgo", None, None, TrafficSource.seo),
        # Referrer-based classification when no UTM is present.
        (None, None, "https://www.google.com/search?q=x", TrafficSource.seo),
        (None, None, "https://news.google.com/article", TrafficSource.seo),
        (None, None, "https://www.facebook.com/post/1", TrafficSource.social),
        (None, None, "https://t.co/abc123", TrafficSource.social),
        # No signal at all.
        (None, None, None, TrafficSource.direct),
        (None, None, "https://some-blog.example/post", TrafficSource.direct),
        # Unknown UTM source with non-paid medium stays direct.
        ("random-partner", "banner", None, TrafficSource.direct),
    ],
)
async def test_classify_traffic_source(utm_source, utm_medium, referrer, expected):
    assert classify_traffic_source(utm_source, utm_medium, referrer) is expected


# ---- device detection --------------------------------------------------------


@pytest.mark.parametrize(
    "user_agent,expected",
    [
        (None, DeviceType.other),
        ("", DeviceType.other),
        ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)", DeviceType.mobile),
        ("Mozilla/5.0 (Linux; Android 14; Pixel 8)", DeviceType.mobile),
        ("Mozilla/5.0 (iPad; CPU OS 17_0)", DeviceType.tablet),
        ("Mozilla/5.0 (Linux; Tablet)", DeviceType.tablet),
        ("Mozilla/5.0 (Windows NT 10.0; Win64; x64)", DeviceType.desktop),
        ("curl/8.0", DeviceType.desktop),
    ],
)
async def test_detect_device_type(user_agent, expected):
    assert detect_device_type(user_agent) is expected


# ---- UTM extraction ----------------------------------------------------------


async def test_extract_utm_params_reads_all_five_and_nulls_missing():
    request = _request(query="utm_source=google&utm_campaign=diwali&utm_content=card")

    params = extract_utm_params(request)

    assert params == {
        "utm_source": "google",
        "utm_medium": None,
        "utm_campaign": "diwali",
        "utm_content": "card",
        "utm_term": None,
    }


async def test_extract_utm_params_treats_blank_as_none():
    request = _request(query="utm_source=")

    assert extract_utm_params(request)["utm_source"] is None


# ---- get_or_create_session (DB-backed) --------------------------------------


async def test_get_or_create_session_creates_a_row(async_session_maker):
    request = _request(
        query="utm_source=google&utm_medium=cpc&utm_campaign=summer",
        headers={"user-agent": "Mozilla/5.0 (iPhone)"},
    )
    response = Response()

    async with async_session_maker() as db:
        session = await get_or_create_session(request, response, db)

    assert session.id
    assert len(session.visitor_id) == 64
    assert session.utm_source == "google"
    assert session.utm_campaign == "summer"
    assert session.device_type is DeviceType.mobile
    assert session.traffic_source is TrafficSource.paid
    assert session.landing_path == "/api/v1/coupons"
    # The session cookie is set on the response.
    assert SESSION_COOKIE in response.headers["set-cookie"]


async def test_get_or_create_session_captures_referrer_domain(async_session_maker):
    request = _request(
        headers={"referer": "https://www.google.com/search?q=shoes"},
    )
    response = Response()

    async with async_session_maker() as db:
        session = await get_or_create_session(request, response, db)

    assert session.referrer_domain == "google.com"
    assert session.traffic_source is TrafficSource.seo


async def test_get_or_create_session_returns_existing_row(async_session_maker):
    existing_id = str(uuid4())
    visitor_id = generate_visitor_id()

    async with async_session_maker() as db:
        db.add(
            AnalyticsSession(
                id=UUID(existing_id),
                visitor_id=visitor_id,
                started_at=datetime.now(UTC) - timedelta(minutes=5),
                last_seen_at=datetime.now(UTC) - timedelta(minutes=5),
                device_type=DeviceType.desktop,
                traffic_source=TrafficSource.direct,
            )
        )
        await db.commit()

    request = _request(cookies={SESSION_COOKIE: existing_id, VISITOR_COOKIE: visitor_id})
    response = Response()

    async with async_session_maker() as db:
        session = await get_or_create_session(request, response, db)

    assert str(session.id) == existing_id
    # last_seen_at was bumped.
    assert session.last_seen_at > datetime.now(UTC) - timedelta(minutes=1)


async def test_get_or_create_session_tolerates_unknown_cookie(async_session_maker):
    """A cookie pointing at a vanished session must create a new one, not 500."""
    request = _request(cookies={SESSION_COOKIE: "does-not-exist"})
    response = Response()

    async with async_session_maker() as db:
        session = await get_or_create_session(request, response, db)

    assert isinstance(session.id, UUID)
    assert str(session.id) != "does-not-exist"
    assert SESSION_COOKIE in response.headers["set-cookie"]


async def test_get_or_create_session_well_formed_uuid_that_matches_nothing(
    async_session_maker,
):
    """A well-formed UUID that no row has must also fall through to create."""
    request = _request(cookies={SESSION_COOKIE: str(uuid4())})
    response = Response()

    async with async_session_maker() as db:
        session = await get_or_create_session(request, response, db)

    assert isinstance(session.id, UUID)
    assert SESSION_COOKIE in response.headers["set-cookie"]
