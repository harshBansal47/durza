"""Sentry wiring.

Deliberately a no-op unless `SENTRY_DSN` is set, so local runs and CI never
phone home and never pay the instrumentation cost.

Two choices worth stating:

* **No PII scrubbing by default, but PII is not sent.** Savevero's API handles
  email addresses and passwords. `send_default_pii=False` (the default here) is
  what keeps them out of Sentry; turning it on would ship every user's address
  to a third party on every error, which is a privacy incident and a
  compliance one, not a debugging convenience.
* **`traces_sample_rate` defaults low.** Full tracing on a coupon API is mostly
  noise. Errors are always reported; traces are sampled.
"""

from typing import TYPE_CHECKING, Any, Literal

from app.core.config import get_settings
from app.core.observability import get_logger

if TYPE_CHECKING:
    # Sentry's own types, for checking only. Importing the SDK at runtime
    # here would make `import app.core.sentry` require an optional dependency,
    # which is exactly what this module exists to avoid.
    from sentry_sdk.types import Event

logger = get_logger("couponbase.sentry")

_initialised = False


def init_sentry() -> bool:
    """Idempotently initialise the SDK. Returns True if Sentry is active."""
    global _initialised

    settings = get_settings()
    if not settings.sentry_dsn:
        if _initialised:
            logger.warning("SENTRY_DSN removed at runtime; Sentry stays off until restart")
            _initialised = False
        return False
    if _initialised:
        return True

    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.httpx import HttpxIntegration

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        release=_release(),
        traces_sample_rate=settings.sentry_traces_sample_rate,
        # Email addresses, auth headers and request bodies stay out. Without
        # this, one error on a login route is a list of real user addresses in
        # someone else's database.
        send_default_pii=False,
        integrations=[FastApiIntegration(), HttpxIntegration()],
        # Our own formatter already emits the request id; let Sentry keep it as
        # a searchable tag instead of burying it in the stack trace.
        before_send=_strip_query_secrets,
    )
    _initialised = True
    logger.info(
        "sentry initialised",
        extra={
            "environment": settings.environment,
            "traces_sample_rate": settings.sentry_traces_sample_rate,
        },
    )
    return True


def _release() -> str | None:
    """Best-effort version string: CI tag, else git sha, else None.

    Imported lazily and wrapped, because a deployed container has no `.git` and
    a local checkout has no CI env. A missing release degrades grouping in
    Sentry; it must never stop the process from booting.
    """
    import os

    for var in ("SENTRY_RELEASE", "GIT_SHA", "GITHUB_SHA"):
        value = os.environ.get(var)
        if value:
            return value
    return None


def _strip_query_secrets(event: "Event", hint: dict[str, Any]) -> "Event | None":
    """Drop query strings from request URLs before the event leaves.

    Deal URLs are the whole product, and some of them carry a subid or a token.
    Sentry's default keeps the full URL, which would mean shipping store
    tracking parameters to a third party.

    `hint` is part of the signature and deliberately unused. Sentry calls
    `before_send(event, hint)`; declaring one argument is not a shorthand, it is
    a different callable, and the mismatch surfaces as a TypeError on the first
    error in production rather than as a type error in the editor.
    """
    request = event.get("request")
    if not isinstance(request, dict):
        return event

    query = request.get("query_string")
    if query:
        request["query_string"] = ""
    url = request.get("url")
    if isinstance(url, str) and "?" in url:
        request["url"] = url.split("?", 1)[0]
    return event


def capture_exception(error: BaseException, **context: object) -> None:
    """Report a non-request-scoped error (a scheduled job, an ingestion run)."""
    if not _initialised:
        return
    import sentry_sdk

    with sentry_sdk.push_scope() as scope:
        for key, value in context.items():
            scope.set_extra(str(key), value)
        sentry_sdk.capture_exception(error)


def capture_message(
    message: str,
    *,
    level: Literal["fatal", "critical", "error", "warning", "info", "debug"] = "error",
    **context: object,
) -> None:
    """Report a condition with no exception attached.

    Needed for the one class of problem an exception report cannot express: a job
    that has failed repeatedly and is now being retried on a long backoff. The
    final exception is not the interesting event — the *pattern* is, and a
    pattern has no traceback.
    """
    if not _initialised:
        return
    import sentry_sdk

    with sentry_sdk.push_scope() as scope:
        for key, value in context.items():
            scope.set_extra(str(key), value)
        sentry_sdk.capture_message(message, level=level)
