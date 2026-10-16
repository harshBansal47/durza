"""Visitor identity and session management.

Privacy-conscious anonymous identification using first-party cookies.

Two cookies are used:
- `cz_vid`: Visitor ID — a cryptographically random opaque identifier.
  Long-lived (1 year). No PII, no email, no phone, no IP address.
- `cz_sid`: Session ID — a cryptographically random opaque identifier.
  Session cookie (expires when browser closes). Links to AnalyticsSession.

The visitor_id is NOT derived from IP address, email, or any other PII.
It is a random UUID stored in a first-party cookie. If the cookie is
cleared or blocked, a new visitor_id is generated — this is expected
and handled gracefully.

UTM parameters are captured at session start and never overwritten,
preserving the original acquisition data for first-touch attribution.
"""

import secrets
from datetime import UTC, datetime
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import Response

from app.models.analytics_session import AnalyticsSession, DeviceType, TrafficSource

# Cookie names
VISITOR_COOKIE = "cz_vid"
SESSION_COOKIE = "cz_sid"

# Cookie settings
VISITOR_COOKIE_MAX_AGE = 365 * 24 * 60 * 60  # 1 year
SESSION_COOKIE_MAX_AGE = None  # Session cookie (expires when browser closes)

# UTM parameter names
UTM_PARAMS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")

# Known social platforms for traffic source classification
SOCIAL_DOMAINS = {
    "facebook.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "linkedin.com",
    "pinterest.com",
    "tiktok.com",
    "youtube.com",
    "reddit.com",
    "t.co",
}

# Known search engines for traffic source classification
SEARCH_ENGINES = {
    "google.com",
    "bing.com",
    "yahoo.com",
    "duckduckgo.com",
    "baidu.com",
    "yandex.com",
    "ask.com",
    "aol.com",
}


def generate_visitor_id() -> str:
    """Generate a cryptographically random opaque visitor identifier.

    This is NOT derived from IP, email, or any PII. It is a random
    UUID hex string that serves as an anonymous identifier.
    """
    return secrets.token_hex(32)  # 64 chars


def generate_session_id() -> str:
    """Generate a cryptographically random opaque session identifier."""
    return secrets.token_hex(32)  # 64 chars


def get_or_create_visitor_id(request: Request, response: Response) -> str:
    """Get the visitor ID from the cookie, or create a new one.

    If the cookie is missing or invalid, a new visitor ID is generated
    and set as a first-party cookie on the response.

    NOTE: This function is intentionally NOT called anywhere yet. Visitor
    identification cookies are not strictly required — analytics events are
    correlated via the `x-session-id` header (set by the frontend on every
    API request) and the `AnalyticsSession` table. Creating a long-lived
    `cz_vid` cookie before consent is granted would violate the consent-first
    model: the frontend gates all tracking on an explicit opt-in, and the
    backend should not set tracking cookies until that opt-in exists.

    When consent is wired end-to-end (frontend sends a consent signal or the
    cookie is set only after acceptance), this function can be called from
    the analytics endpoints to establish the visitor identity.
    """
    visitor_id = request.cookies.get(VISITOR_COOKIE)
    if visitor_id and len(visitor_id) == 64:
        return visitor_id

    visitor_id = generate_visitor_id()
    response.set_cookie(
        VISITOR_COOKIE,
        visitor_id,
        max_age=VISITOR_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,  # Set to True in production with HTTPS
    )
    return visitor_id


def get_or_create_session_id(request: Request, response: Response) -> str:
    """Get the session ID from the cookie, or create a new one.

    Session cookies expire when the browser closes.
    """
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id and len(session_id) == 64:
        return session_id

    session_id = generate_session_id()
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=SESSION_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,  # Set to True in production with HTTPS
    )
    return session_id


def classify_traffic_source(
    utm_source: str | None,
    utm_medium: str | None,
    referrer: str | None,
) -> TrafficSource:
    """Classify the traffic source from UTM parameters and referrer.

    Priority:
    1. UTM parameters (most reliable)
    2. Referrer domain analysis
    3. Default to "direct"
    """
    # UTM-based classification
    if utm_source:
        source_lower = utm_source.lower()
        if any(se in source_lower for se in SEARCH_ENGINES):
            return TrafficSource.seo
        if any(sd in source_lower for sd in SOCIAL_DOMAINS):
            return TrafficSource.social
        if source_lower in ("email", "newsletter", "mail"):
            return TrafficSource.email
        if utm_medium and utm_medium.lower() in ("cpc", "cpm", "ppc", "paid"):
            return TrafficSource.paid
        if source_lower in ("google", "bing", "yahoo", "duckduckgo"):
            return TrafficSource.seo

    # Referrer-based classification
    if referrer:
        parsed = urlparse(referrer)
        domain = parsed.netloc.lower()
        # Remove www. prefix
        domain = domain.removeprefix("www.")

        if any(domain == se or domain.endswith("." + se) for se in SEARCH_ENGINES):
            return TrafficSource.seo
        if any(domain == sd or domain.endswith("." + sd) for sd in SOCIAL_DOMAINS):
            return TrafficSource.social

    return TrafficSource.direct


def detect_device_type(user_agent: str | None) -> DeviceType:
    """Coarse device type detection from User-Agent string."""
    if not user_agent:
        return DeviceType.other
    ua = user_agent.lower()
    if "mobile" in ua or "android" in ua or "iphone" in ua:
        return DeviceType.mobile
    if "tablet" in ua or "ipad" in ua:
        return DeviceType.tablet
    return DeviceType.desktop


def extract_utm_params(request: Request) -> dict[str, str | None]:
    """Extract UTM parameters from the request query string."""
    params = {}
    for param in UTM_PARAMS:
        value = request.query_params.get(param)
        params[param] = value if value else None
    return params


async def get_or_create_session(
    request: Request,
    response: Response,
    db_session,
) -> AnalyticsSession:
    """Get or create an analytics session for this request.

    If the session cookie exists and matches a recent session, that session
    is returned. Otherwise, a new session is created with UTM parameters
    and referrer captured from the request.
    """
    from sqlalchemy import select

    session_id = request.cookies.get(SESSION_COOKIE)
    visitor_id = request.cookies.get(VISITOR_COOKIE)

    # Try to find an existing session
    if session_id:
        result = await db_session.execute(
            select(AnalyticsSession).where(AnalyticsSession.id == session_id)
        )
        existing = result.scalar_one_or_none()
        if existing:
            # Update last_seen_at
            existing.last_seen_at = datetime.now(UTC)
            await db_session.commit()
            return existing

    # Create new session
    utm_params = extract_utm_params(request)
    referrer = request.headers.get("referer")
    referrer_domain = None
    if referrer:
        parsed = urlparse(referrer)
        referrer_domain = parsed.netloc.lower()
        referrer_domain = referrer_domain.removeprefix("www.")

    traffic_source = classify_traffic_source(
        utm_params.get("utm_source"),
        utm_params.get("utm_medium"),
        referrer,
    )

    device_type = detect_device_type(request.headers.get("user-agent"))

    session = AnalyticsSession(
        id=session_id or generate_session_id(),
        visitor_id=visitor_id or generate_visitor_id(),
        started_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        landing_page=str(request.url.path),
        landing_path=str(request.url.path),
        referrer=referrer,
        referrer_domain=referrer_domain,
        utm_source=utm_params.get("utm_source"),
        utm_medium=utm_params.get("utm_medium"),
        utm_campaign=utm_params.get("utm_campaign"),
        utm_content=utm_params.get("utm_content"),
        utm_term=utm_params.get("utm_term"),
        device_type=device_type,
        country=None,  # Would be set by geo-IP middleware
        traffic_source=traffic_source,
    )
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)

    # Set session cookie
    response.set_cookie(
        SESSION_COOKIE,
        str(session.id),
        max_age=SESSION_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,  # Set to True in production with HTTPS
    )

    return session
