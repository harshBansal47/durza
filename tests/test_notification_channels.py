"""Email channel selection for saved-item update digests."""

from app.models.saved_items import NotificationPreference
from app.services import notifications


def _prefs(*, email_enabled: bool = False) -> NotificationPreference:
    return NotificationPreference(email_enabled=email_enabled)


def test_no_channels_when_email_is_off() -> None:
    assert notifications.senders_for(_prefs()) == []


def test_email_channel_requires_the_email_toggle() -> None:
    senders = notifications.senders_for(_prefs(email_enabled=True))
    assert [sender.channel for sender in senders] == ["email"]
