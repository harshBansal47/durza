"""Request correlation and latency instrumentation.

Two jobs, both of which are much cheaper to add now than to retrofit:

* **A request id on every request and response.** The id is taken from an
  inbound `X-Request-Id` when present (so a trace started at the Next.js edge
  survives the hop) and echoed back on the response, which is what lets someone
  go from a user-reported failure to the exact log lines without guessing at
  timestamps.
* **Latency and count metrics labelled by route template.** See the note in
  `app.core.metrics` on why this is the *template* and not the path.
"""

import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core import metrics
from app.core.observability import bind_request_id, new_request_id, request_id_var

REQUEST_ID_HEADER = "X-Request-Id"


# A raw path is only ever used when no route matched at all. Using the concrete
# path as the general label would be catastrophic for cardinality: 404-heavy
# paths are attacker-controlled and unbounded, which is the most common way to
# turn a Prometheus scrape into an out-of-memory kill of the metrics server.
_UNMATCHED = "unmatched"


def _route_label(request: Request) -> str:
    """The matched route's path *template*, e.g. `/api/v1/coupons/{coupon_id}`.

    Read from `scope["route"]`, which Starlette populates during routing. That
    runs after this middleware is entered, so the label is resolved on the way
    out of `call_next` rather than on the way in.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if not isinstance(path, str) or not path:
        return _UNMATCHED
    return path


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or new_request_id()
        token = bind_request_id(request_id)
        request.state.request_id = request_id

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Routing has happened by now even on the error path, so the label is
            # still the template and not a raw path.
            route = _route_label(request)
            metrics.http_requests.labels(request.method, route, "500").inc()
            metrics.http_duration.labels(request.method, route).observe(
                time.perf_counter() - started
            )
            raise

        elapsed = time.perf_counter() - started
        response.headers[REQUEST_ID_HEADER] = request_id

        route = _route_label(request)
        metrics.http_requests.labels(request.method, route, str(response.status_code)).inc()
        metrics.http_duration.labels(request.method, route).observe(elapsed)
        # Reset last, so anything downstream of this middleware — including the
        # exception handlers and the response rendering above — still sees the
        # id, but an unrelated request never inherits it if this task is reused.
        request_id_var.reset(token)
        return response
