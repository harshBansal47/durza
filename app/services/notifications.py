"""Notification delivery. Channels are pluggable.

A channel with no credentials configured logs and reports success, so the alert
engine is fully exercisable locally with no mail server and no VAPID keys. Once
credentials exist, delivery is real: aiosmtplib for email, pywebpush for push.

Senders take an `AlertContent` rather than a bare string because "send this
text" cannot produce a subject line, a preheader, a price and a call to action
— the formatting *is* the product here.
"""

import asyncio
from typing import Protocol

import httpx

from app.core.config import get_settings
from app.core.observability import get_logger
from app.models.tracking import NotificationPreference
from app.models.user import User
from app.services import email as email_service
from app.services import push as push_service
from app.services.email import AlertContent

logger = get_logger("couponza.notifications")


class Sender(Protocol):
    channel: str

    async def send(
        self, user: User, prefs: NotificationPreference, alert: AlertContent
    ) -> bool: ...


class EmailSender:
    channel = "email"

    async def send(self, user: User, prefs: NotificationPreference, alert: AlertContent) -> bool:
        return await email_service.send_email(user.email, alert)


class TelegramSender:
    channel = "telegram"

    async def send(self, user: User, prefs: NotificationPreference, alert: AlertContent) -> bool:
        token = get_settings().telegram_bot_token
        if not token or not prefs.telegram_chat_id:
            logger.info(
                "telegram queued (TELEGRAM_BOT_TOKEN or chat_id missing)",
                extra={"subject": alert.subject},
            )
            return True

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": prefs.telegram_chat_id,
                    "text": f"{alert.headline}\n\n{alert.detail}\n\n{alert.cta_url}",
                },
            )
        if resp.status_code != 200:
            logger.warning("telegram send failed", extra={"status": resp.status_code})
            return False
        return True


class PushSender:
    channel = "push"

    async def send(self, user: User, prefs: NotificationPreference, alert: AlertContent) -> bool:
        """Raises PushSubscriptionGone when the browser has unsubscribed.

        Propagating rather than swallowing is deliberate: the caller owns the
        session and can durably clear the dead subscription, so it stops being
        retried on every future alert. Silently dropping it here would leave a
        table of tombstones that we keep paying to call.
        """

        def deliver() -> bool:
            return push_service.send_push(
                prefs.push_subscription,
                push_service.PushPayload(
                    title=alert.headline,
                    body=alert.detail,
                    url=alert.cta_url,
                ),
            )

        # pywebpush is synchronous; a thread keeps a burst of pushes from
        # blocking the event loop that is delivering everyone else's alerts.
        return await asyncio.to_thread(deliver)


def senders_for(prefs: NotificationPreference) -> list[Sender]:
    out: list[Sender] = []
    if prefs.email_enabled:
        out.append(EmailSender())
    if prefs.telegram_enabled and prefs.telegram_chat_id:
        out.append(TelegramSender())
    if prefs.push_enabled and prefs.push_subscription:
        out.append(PushSender())
    return out
