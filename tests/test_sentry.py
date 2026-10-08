"""Sentry wiring.

The SDK is a no-op unless a DSN is configured, which is what keeps local runs and
CI from phoning home. That makes the module easy to under-test, so these tests
pin down the two properties that only fail in production: the callback signature
and the fact that PII stays out.
"""

import pytest

from app.core import sentry
from app.core.sentry import _release, _strip_query_secrets


@pytest.fixture(autouse=True)
def _reset_initialisation():
    """The module-level `_initialised` flag is process-global.

    Without this, whichever test happens to call `init_sentry` first decides
    whether every later test sees a live or a dead SDK, and the suite passes or
    fails based on file ordering.
    """
    sentry._initialised = False
    yield
    sentry._initialised = False


# ---- Callback signature ----


def test_before_send_accepts_the_two_arguments_sentry_actually_passes() -> None:
    # `before_send(event, hint)`. A one-argument callable is not a shorthand for
    # that, it is a different function, and mypy accepting it does not make the
    # SDK call it: the mismatch shows up as a TypeError on the first error in
    # production, at which point the hook that was supposed to redact query
    # strings never runs and the sensitive URL is sent anyway.
    import inspect

    assert len(inspect.signature(_strip_query_secrets).parameters) == 2


def test_before_send_drops_the_query_string() -> None:
    # Deal URLs are the product, and some carry a subid or a token. Sentry's
    # default keeps the whole URL.
    event = {
        "request": {
            "url": "https://api.BachatBase.example/go?subid=aff-9182&uid=user-42",
            "query_string": "subid=aff-9182&uid=user-42",
        }
    }
    result = _strip_query_secrets(event, {})

    assert result["request"]["url"] == "https://api.BachatBase.example/go"
    assert result["request"]["query_string"] == ""


def test_before_send_leaves_a_url_without_a_query_string_alone() -> None:
    # Idempotence matters: this runs on every event, and a mangled URL would
    # break grouping for the requests that carry no secrets at all.
    event = {"request": {"url": "https://api.BachatBase.example/health", "query_string": ""}}
    assert _strip_query_secrets(event, {})["request"]["url"] == (
        "https://api.BachatBase.example/health"
    )


def test_before_send_passes_through_an_event_with_no_request() -> None:
    # Scheduled jobs and ingestion failures produce events with no request at
    # all. Returning None here would silently discard them.
    assert _strip_query_secrets({"message": "job failed"}, {}) == {"message": "job failed"}


def test_before_send_passes_through_a_request_that_is_not_a_dict() -> None:
    assert _strip_query_secrets({"request": "weird"}, {}) == {"request": "weird"}


# ---- No-op behaviour ----


def test_init_is_a_no_op_without_a_dsn() -> None:
    assert sentry.init_sentry() is False
    assert sentry._initialised is False


def test_capture_is_a_no_op_when_uninitialised() -> None:
    # Every call site checks nothing and just calls this. If it raised here, a
    # missing DSN would turn every job failure into a crash.
    sentry.capture_exception(RuntimeError("boom"), job="send_alerts")
    sentry.capture_message("dead-lettered", job="send_alerts")


def test_init_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    # `lifespan` runs on every worker start, and on a reload. Re-initialising a
    # live SDK stacks a second set of integrations and doubles every event.
    #
    # `sentry_sdk.init` is stubbed rather than called for real: the real one
    # patches the stdlib logging module process-wide and immediately tries to
    # ship an envelope to the DSN, so a test that calls it leaks global state
    # into the rest of the suite and makes a network request to do it.
    from app.core.config import get_settings

    settings = get_settings()
    original = settings.sentry_dsn
    calls: list[dict] = []
    monkeypatch.setattr("sentry_sdk.init", lambda **kwargs: calls.append(kwargs), raising=True)
    settings.sentry_dsn = "https://public@sentry.example/1"
    try:
        assert sentry.init_sentry() is True
        assert sentry.init_sentry() is True
        assert sentry._initialised is True
    finally:
        settings.sentry_dsn = original
        sentry._initialised = False

    assert len(calls) == 1, "the SDK was initialised more than once"


def test_a_configured_dsn_scrubs_pii_and_query_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The two settings that decide what leaves the process. Asserted on the
    # arguments handed to the SDK, because both are silent when wrong: PII on,
    # and a hook that never fires.
    from app.core.config import get_settings

    settings = get_settings()
    original = settings.sentry_dsn
    captured: dict = {}
    monkeypatch.setattr("sentry_sdk.init", lambda **kwargs: captured.update(kwargs))

    settings.sentry_dsn = "https://public@sentry.example/1"
    try:
        sentry.init_sentry()
    finally:
        settings.sentry_dsn = original
        sentry._initialised = False

    assert captured["send_default_pii"] is False
    assert callable(captured["before_send"])
    # And the hook is the one that actually strips query strings.
    assert (
        captured["before_send"](
            {"request": {"url": "https://api.example/go?uid=user-42", "query_string": "uid=x"}},
            {},
        )["request"]["url"]
        == "https://api.example/go"
    )


# ---- Release ----


def test_release_falls_back_to_the_git_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_SHA", "deadbeef")
    monkeypatch.delenv("SENTRY_RELEASE", raising=False)
    assert _release() == "deadbeef"


def test_release_is_none_outside_a_deploy(monkeypatch: pytest.MonkeyPatch) -> None:
    # A deployed container has no `.git`, so this must not raise — a missing
    # release costs grouping in Sentry, and must never stop the process booting.
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.delenv("SENTRY_RELEASE", raising=False)
    monkeypatch.chdir("/")
    assert _release() is None
