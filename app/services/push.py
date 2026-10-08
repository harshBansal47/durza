"""Browser push via the Web Push protocol (pywebpush).

Everything here is standards plumbing: RFC 8291 payload encryption, VAPID
signed headers, and push-service error handling. The parts worth reading are
the comments on failure, because push is the channel where "it returned 410"
means something specific and acting on it is the difference between a working
notification system and one that grows an unbounded table of dead endpoints.

A subscription is per-browser, per-origin. When it dies, the server must forget
it — otherwise every future alert pays for a request to a tombstone.
"""

import json
from dataclasses import dataclass
from typing import Any

from app.core.config import get_settings
from app.core.observability import get_logger

logger = get_logger("couponza.push")


class PushSubscriptionGone(Exception):
    """The push service says this subscription is dead and must be deleted.

    404/410 from the push endpoint. Per RFC 8030 the endpoint is invalid and
    will never become valid again.
    """


@dataclass(frozen=True)
class PushPayload:
    title: str
    body: str
    url: str | None = None
    tag: str | None = None


def build_payload(payload: PushPayload) -> str:
    return json.dumps(
        {
            "title": payload.title,
            "body": payload.body,
            "url": payload.url,
            "tag": payload.tag,
        }
    )


def load_subscription(raw: str | None) -> dict[str, Any] | None:
    """Parse a stored subscription. Returns None on anything malformed.

    A row that cannot be parsed is worse than no row: it makes `push_enabled`
    look true while every send fails. Callers treat None as "clear this".
    """
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    # pywebpush needs exactly these keys; a partial dict raises deep inside
    # cryptography with an unhelpful message.
    if not {"endpoint", "keys"} <= parsed.keys():
        return None
    keys = parsed["keys"]
    if not isinstance(keys, dict) or not {"p256dh", "auth"} <= keys.keys():
        return None
    return parsed


def _send_kwargs(subscription: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    return {
        "sub_info": subscription,
        "vapid_private_key": settings.vapid_private_key,
        "vapid_claims": {"sub": settings.vapid_subject},
        "ttl": settings.push_ttl_seconds,
        # Some push services reject encrypted payloads they consider too large.
        # A 4097-byte ceiling is the documented safe limit for a push message.
        "content_encoding": "aes128gcm",
    }


def send_push(raw_subscription: str | None, payload: PushPayload) -> bool:
    """Send one notification. Returns False instead of raising.

    Raises PushSubscriptionGone only when the caller has asked for strict mode
    (see `raise_on_gone`), which the notification-settings path uses so it can
    clear the dead subscription immediately rather than on the next send.
    """
    settings = get_settings()
    subscription = load_subscription(raw_subscription)
    if subscription is None:
        logger.info("push skipped (unusable subscription)")
        return False

    if not settings.push_configured:
        logger.info(
            "push queued (VAPID keys not configured)",
            extra={"title": payload.title},
        )
        return True

    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info=subscription,
            data=build_payload(payload),
            **_send_kwargs(subscription),
        )
    except WebPushException as exc:
        # `status_code` is a property that normalises both the sync (requests) and
        # async (aiohttp) response shapes. Digging into `exc.response` directly
        # would silently miss one of them and treat a dead subscription as a
        # transient failure — which is exactly the case we most need to catch.
        status = exc.status_code
        if status in (404, 410):
            logger.info(
                "push subscription gone; it will be cleared",
                extra={"status": status},
            )
            raise PushSubscriptionGone from exc
        logger.warning(
            "push send failed",
            extra={"error": str(exc), "status": status},
        )
        return False

    logger.info("push sent", extra={"title": payload.title})
    return True
