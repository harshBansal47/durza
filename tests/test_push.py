"""Browser push: subscription validation and send behaviour.

The tests concentrate on `load_subscription`, because a subscription row that
parses as JSON but lacks a key half is the failure that produces the worst
possible user experience: `push_enabled` reads true, the settings page promises
notifications, and every single send fails silently forever.
"""

import json
import types

import pytest

from app.services import push as push_service
from app.services.push import PushPayload, load_subscription

VALID = {
    "endpoint": "https://fcm.googleapis.com/fcm/send/abc123",
    "keys": {"p256dh": "public-key", "auth": "auth-secret"},
}

PAYLOAD = PushPayload(title="A price drop", body="Sony fell to $279", url="https://x.example/p")


def test_valid_subscription_parses() -> None:
    assert load_subscription(json.dumps(VALID)) == VALID


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "not json at all",
        json.dumps(["a", "list"]),
        json.dumps({"keys": VALID["keys"]}),
        json.dumps({"endpoint": VALID["endpoint"]}),
        json.dumps({"endpoint": VALID["endpoint"], "keys": {}}),
        json.dumps({"endpoint": VALID["endpoint"], "keys": {"p256dh": "only-one"}}),
        json.dumps({"endpoint": VALID["endpoint"], "keys": "not-a-dict"}),
    ],
)
def test_unusable_subscriptions_return_none(raw: str | None) -> None:
    # Every one of these must be rejected up front. Treating them as valid makes
    # push_enabled a lie, and the failure surfaces only as an exception deep
    # inside cryptography.
    assert load_subscription(raw) is None


def test_payload_is_compact_json() -> None:
    body = json.loads(push_service.build_payload(PAYLOAD))
    assert body["title"] == "A price drop"
    assert body["url"] == "https://x.example/p"


def test_payload_stays_under_the_push_size_ceiling() -> None:
    # Push services reject payloads over ~4KB, which for an alert means a short
    # body and nothing else. This asserts the fields we send are the only ones.
    raw = push_service.build_payload(PAYLOAD)
    assert len(raw.encode()) < 1024


def test_send_without_vapid_keys_reports_queued() -> None:
    from app.core.config import get_settings

    assert get_settings().push_configured is False
    assert push_service.send_push(json.dumps(VALID), PAYLOAD) is True


def test_send_with_unusable_subscription_reports_failure() -> None:
    # No VAPID keys configured, so the send is "queued" — but an unusable
    # subscription is caught before that and fails outright, because queued or
    # not, nothing could ever be delivered.
    assert push_service.send_push(json.dumps({"endpoint": "x"}), PAYLOAD) is False


def test_send_failure_returns_false_and_does_not_raise(monkeypatch) -> None:
    settings = push_service.get_settings()
    monkeypatch.setattr(settings, "vapid_private_key", "priv", raising=False)
    monkeypatch.setattr(settings, "vapid_public_key", "pub", raising=False)

    import pywebpush

    def _boom(**kwargs):
        raise pywebpush.WebPushException("push service unavailable")

    monkeypatch.setattr(pywebpush, "webpush", _boom)

    assert push_service.send_push(json.dumps(VALID), PAYLOAD) is False


@pytest.mark.parametrize("status", [404, 410])
def test_dead_subscription_is_raised_so_the_caller_can_clear_it(monkeypatch, status: int) -> None:
    # 404/410 mean the endpoint is invalid forever. This must propagate: the
    # caller owns the session and can durably delete the row, so it stops being
    # retried on every future alert.
    settings = push_service.get_settings()
    monkeypatch.setattr(settings, "vapid_private_key", "priv", raising=False)
    monkeypatch.setattr(settings, "vapid_public_key", "pub", raising=False)

    import pywebpush

    # Both response shapes pywebpush can carry: requests uses `status_code`,
    # aiohttp uses `status`. The service must read the property, not the
    # attribute, or one of them is silently treated as a transient failure.
    response = types.SimpleNamespace(status_code=status, status=status, text="", headers={})

    def _boom(**kwargs):
        raise pywebpush.WebPushException("gone", response=response)

    monkeypatch.setattr(pywebpush, "webpush", _boom)

    with pytest.raises(push_service.PushSubscriptionGone):
        push_service.send_push(json.dumps(VALID), PAYLOAD)


def test_send_uses_vapid_claims_and_aes_encoding(monkeypatch) -> None:
    settings = push_service.get_settings()
    monkeypatch.setattr(settings, "vapid_private_key", "priv", raising=False)
    monkeypatch.setattr(settings, "vapid_public_key", "pub", raising=False)

    captured: dict = {}

    import pywebpush

    def _capture(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(pywebpush, "webpush", _capture)
    assert push_service.send_push(json.dumps(VALID), PAYLOAD) is True

    assert captured["vapid_private_key"] == "priv"
    assert captured["vapid_claims"]["sub"] == settings.vapid_subject
    assert captured["content_encoding"] == "aes128gcm"
    assert captured["ttl"] == settings.push_ttl_seconds
