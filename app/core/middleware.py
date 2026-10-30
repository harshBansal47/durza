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

import logging
import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core import metrics
from app.core.config import get_settings
from app.core.observability import bind_request_id, new_request_id, request_id_var

REQUEST_ID_HEADER = "X-Request-Id"

# One line per request, carrying the request id. Metrics say how many and how
# slow; this says which one. Uvicorn's own access log is switched off in favour
# of it (see `configure_logging`), so without this nothing records a request.
access_logger = logging.getLogger("durza.access")

# Probes and scrapes fire constantly and say nothing worth keeping.
_QUIET_ROUTES = frozenset({"/health", "/metrics"})


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


def _log_request(request: Request, route: str, status_code: int, elapsed: float) -> None:
    if route in _QUIET_ROUTES:
        return
    if status_code >= 500:
        level = logging.ERROR
    elif elapsed >= get_settings().slow_request_seconds:
        level = logging.WARNING
    else:
        level = logging.INFO
    fields: dict[str, object] = {
        "http_method": request.method,
        "http_route": route,
        "http_status": status_code,
        "duration_ms": round(elapsed * 1000),
    }
    if route == _UNMATCHED:
        # No template to group by, so keep a clipped path: 404 probes are
        # exactly what someone reading these logs wants to see.
        fields["http_path"] = request.url.path[:200]
    access_logger.log(level, "%s %s %s", request.method, route, status_code, extra=fields)


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
            _log_request(request, route, 500, time.perf_counter() - started)
            raise

        elapsed = time.perf_counter() - started
        response.headers[REQUEST_ID_HEADER] = request_id

        route = _route_label(request)
        metrics.http_requests.labels(request.method, route, str(response.status_code)).inc()
        metrics.http_duration.labels(request.method, route).observe(elapsed)
        _log_request(request, route, response.status_code, elapsed)
        # Reset last, so anything downstream of this middleware — including the
        # exception handlers and the response rendering above — still sees the
        # id, but an unrelated request never inherits it if this task is reused.
        request_id_var.reset(token)
        return response
