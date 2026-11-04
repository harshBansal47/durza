"""`Cache-Control` for the public, read-only catalogue endpoints.

Until now no public response carried any caching header, so every browser, proxy and CDN had
to treat each catalogue read as uncacheable. This adds them, but only for an explicit
allow-list: a route is cacheable because someone decided it is, never because it was not
excluded. Everything else (accounts, admin, tracked `/go` redirects, vote counts, anything
personalised, anything an `Authorization` header or a `Set-Cookie` touched) is left exactly
as it was.

Two lifetimes:
  * offers and prices change through the day, so they get a short window;
  * stores, categories, pages and editorial change when an editor publishes.

`stale-while-revalidate` lets a cache answer instantly and refresh in the background.
"""

import re
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

API_PREFIX = "/api/v1"

OFFERS = "public, max-age=30, s-maxage=60, stale-while-revalidate=120"
CATALOGUE = "public, max-age=60, s-maxage=300, stale-while-revalidate=600"

_SEG = r"[^/]+"

# Deliberately absent: `/coupons/{id}` and `/coupons/{id}/verification-history` (live vote
# counts: the storefront reads them uncached on purpose), every `/go`, `/autocomplete` and
# `/search/suggest` (high-cardinality, already uncached client-side), `/ads` (rotation),
# `/editorial/manage` and `/editorial/id/...` (editor tooling), and everything under
# `/me`, `/auth`, `/account`, `/admin`, `/analytics` and `/personalization`.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), value)
    for pattern, value in (
        (r"^/coupons$", OFFERS),
        (rf"^/coupons/by-slug/{_SEG}$", OFFERS),
        (rf"^/stores/by-slug/{_SEG}/destination$", OFFERS),
        (rf"^/products/{_SEG}/price-history$", OFFERS),
        (r"^/products$", OFFERS),
        (rf"^/products/by-slug/{_SEG}$", OFFERS),
        (r"^/stores$", CATALOGUE),
        (rf"^/stores/by-slug/{_SEG}$", CATALOGUE),
        (r"^/stores/markets$", CATALOGUE),
        (r"^/categories$", CATALOGUE),
        (rf"^/categories/by-slug/{_SEG}$", CATALOGUE),
        (r"^/pages$", CATALOGUE),
        (rf"^/pages/by-slug/{_SEG}$", CATALOGUE),
        (r"^/editorial$", CATALOGUE),
        (r"^/editorial/homepage$", CATALOGUE),
        (rf"^/editorial/(?!manage/|id/){_SEG}/{_SEG}$", CATALOGUE),
        (r"^/sales$", CATALOGUE),
        (rf"^/sales/{_SEG}(/{_SEG})?$", CATALOGUE),
    )
)


def cache_control_for(path: str) -> str | None:
    """The `Cache-Control` value for an API path, or None when it must not be cached."""
    if not path.startswith(API_PREFIX):
        return None
    relative = path[len(API_PREFIX) :]
    for pattern, value in _RULES:
        if pattern.match(relative):
            return value
    return None


class CacheControlMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if (
            request.method in {"GET", "HEAD"}
            and response.status_code == 200
            and "cache-control" not in response.headers
            and "set-cookie" not in response.headers
            and "authorization" not in request.headers
        ):
            value = cache_control_for(request.url.path)
            if value is not None:
                response.headers["Cache-Control"] = value
        return response
