"""Private account alerts are delivered by email only.

Public Telegram offer posts are handled separately by `telegram_service`; they
are not a private account notification channel.
"""

from typing import Protocol

from app.models.saved_items import NotificationPreference
from app.models.user import User
from app.services import email as email_service
from app.services.email import AlertContent


class Sender(Protocol):
    channel: str

    async def send(
        self, user: User, prefs: NotificationPreference, alert: AlertContent
    ) -> bool: ...


class EmailSender:
    channel = "email"

    async def send(self, user: User, prefs: NotificationPreference, alert: AlertContent) -> bool:
        return await email_service.send_email(user.email, alert)


def senders_for(prefs: NotificationPreference) -> list[Sender]:
    return [EmailSender()] if prefs.email_enabled else []
