"""Health, deep readiness and the metrics endpoint.

`/health` and `/health/deep` are separate because they are asked by different
things at very different rates, and the expensive one must not be able to turn a
database blip into a restart storm.
"""

import re
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from app.core import metrics
from app.core.config import get_settings
from app.models.job import JobRun, JobStatus
from app.services import scheduler

# ---- Liveness ----


@pytest.mark.asyncio
async def test_health_is_ok(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_health_does_not_touch_the_database(client: AsyncClient) -> None:
    # The load balancer polls this thousands of times a minute. If it depended
    # on Postgres, a slow database would fail every replica at once.
    from app.routers.api.v1 import system

    assert system.health.__doc__ is not None
    response = await client.get("/health")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_health_returns_the_environment(client: AsyncClient) -> None:
    assert (await client.get("/health")).json()["environment"] == get_settings().environment


# ---- Request correlation ----


@pytest.mark.asyncio
async def test_response_carries_a_request_id(client: AsyncClient) -> None:
    # This is what lets someone go from "the page broke" to the exact log lines
    # without guessing at timestamps.
    assert (await client.get("/health")).headers["x-request-id"]


@pytest.mark.asyncio
async def test_an_inbound_request_id_is_echoed_back(client: AsyncClient) -> None:
    # So a trace started at the Next.js edge survives the hop to the API.
    response = await client.get("/health", headers={"X-Request-Id": "edge-trace-123"})
    assert response.headers["x-request-id"] == "edge-trace-123"


@pytest.mark.asyncio
async def test_each_request_gets_a_distinct_id(client: AsyncClient) -> None:
    one = (await client.get("/health")).headers["x-request-id"]
    two = (await client.get("/health")).headers["x-request-id"]
    assert one != two


# ---- Deep readiness ----


@pytest.mark.asyncio
async def test_deep_health_reports_ok_when_the_database_works(client: AsyncClient) -> None:
    body = (await client.get("/health/deep")).json()
    assert body["status"] == "ok"
    assert body["checks"]["database"]["status"] == "ok"
    assert "latency_ms" in body["checks"]["database"]


@pytest.mark.asyncio
async def test_deep_health_reports_a_job_that_has_never_run_as_normal(
    client: AsyncClient,
) -> None:
    # The scheduler is off by default, so "never run" is the expected state
    # locally and in CI. Calling that unhealthy would train everyone to ignore
    # this endpoint.
    body = (await client.get("/health/deep")).json()
    assert body["checks"]["send_alerts"]["status"] == "never run"
    assert body["status"] == "ok"


@pytest.mark.asyncio
async def test_deep_health_flags_a_stale_job(client: AsyncClient, async_session_maker) -> None:
    async with async_session_maker() as db:
        db.add(
            JobRun(
                job="send_alerts",
                status=JobStatus.success,
                started_at=datetime.now(UTC) - timedelta(days=2),
                detail="sent 0 alert(s)",
            )
        )
        await db.commit()

    body = (await client.get("/health/deep")).json()
    assert body["checks"]["send_alerts"]["status"] == "stale"
    # A stale job is worth reporting but is not a reason to stop serving traffic:
    # the database is fine and every page still renders.
    assert body["status"] == "ok"


@pytest.mark.asyncio
async def test_deep_health_reports_the_last_failure_detail(
    client: AsyncClient, async_session_maker
) -> None:
    async with async_session_maker() as db:
        db.add(
            JobRun(
                job="refresh_prices",
                status=JobStatus.failed,
                started_at=datetime.now(UTC) - timedelta(minutes=10),
                detail="RuntimeError: adapter exploded",
            )
        )
        await db.commit()

    check = (await client.get("/health/deep")).json()["checks"]["refresh_prices"]
    assert check["status"] == "last run failed"
    assert check["detail"] == "RuntimeError: adapter exploded"


@pytest.mark.asyncio
async def test_deep_health_ignores_skipped_rows(client: AsyncClient, async_session_maker) -> None:
    # On a three-replica deployment most rows are `skipped`. Reading one as the
    # job's latest state would report a healthy job as never run.
    async with async_session_maker() as db:
        db.add(
            JobRun(
                job="send_alerts",
                status=JobStatus.skipped,
                started_at=datetime.now(UTC) - timedelta(days=5),
                detail="another worker held the lock",
            )
        )
        await db.commit()

    assert (await client.get("/health/deep")).json()["checks"]["send_alerts"][
        "status"
    ] == "never run"


@pytest.mark.asyncio
async def test_deep_health_reports_delivery_configuration(client: AsyncClient) -> None:
    checks = (await client.get("/health/deep")).json()["checks"]
    assert checks["email"] == {"status": "not configured"}
    assert checks["push"] == {"status": "not configured"}


@pytest.mark.asyncio
async def test_deep_health_returns_503_when_the_database_is_down(client: AsyncClient) -> None:
    # So orchestrators actually stop routing traffic here — but the body still
    # describes every other check, because a partial failure report reads like
    # a pass and is worse than a failure.
    from sqlalchemy.exc import OperationalError

    from app.core.database import get_db
    from app.main import app

    class _BrokenSession:
        """Fails every query, the way an unreachable Postgres does."""

        bind = None

        async def execute(self, *args, **kwargs):
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

        async def commit(self):
            pass

    async def _override_get_db():
        yield _BrokenSession()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        response = await client.get("/health/deep")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        # The error is reported by type, not by message: an exception string can
        # carry the DSN, and this endpoint is unauthenticated.
        assert body["checks"]["database"]["status"] == "error"
        assert "connection refused" not in response.text
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---- Metrics ----


@pytest.mark.asyncio
async def test_metrics_is_404_when_disabled(client: AsyncClient) -> None:
    # Off by default so a stray scrape isn't talking to a laptop.
    assert get_settings().metrics_enabled is False
    assert (await client.get("/metrics")).status_code == 404


@pytest.mark.asyncio
async def test_metrics_renders_when_enabled(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "metrics_enabled", True, raising=False)
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "couponbase_http_requests_total" in response.text


@pytest.mark.asyncio
async def test_requests_are_counted_by_route_template_not_path(
    client: AsyncClient, monkeypatch
) -> None:
    # Labelling by concrete path is the most common way to turn a scrape into an
    # OOM kill, because 404-heavy paths are attacker-controlled and unbounded.
    monkeypatch.setattr(get_settings(), "metrics_enabled", True, raising=False)
    before = await _sample_count(client, "/health", "200")

    await client.get("/health")
    await client.get("/health")
    await client.get("/does-not-exist-at-all")

    assert await _sample_count(client, "/health", "200") == before + 2
    # Unmatched requests collapse onto one label instead of one label each.
    assert 'route="unmatched"' in await body_of(client)


@pytest.mark.asyncio
async def test_metrics_include_the_stale_coupon_gauge(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "metrics_enabled", True, raising=False)
    assert "couponbase_stale_coupons" in await body_of(client)


def test_metric_registry_is_not_the_global_default() -> None:
    # A private registry keeps test runs from leaking counters into each other.
    assert metrics.registry is not None
    payload, _ = metrics.render()
    assert b"couponbase_job_runs_total" in payload


# ---- Push endpoints ----


@pytest.mark.asyncio
async def test_push_key_is_readable_without_auth(client: AsyncClient) -> None:
    # The key is public by definition and the settings page needs it before login
    # completes, to decide whether to offer the toggle at all.
    response = await client.get("/api/v1/me/push/key")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["public_key"] is None


@pytest.mark.asyncio
async def test_push_key_reports_the_configured_key(client: AsyncClient, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "vapid_public_key", "pub-key", raising=False)
    monkeypatch.setattr(settings, "vapid_private_key", "priv-key", raising=False)

    body = (await client.get("/api/v1/me/push/key")).json()
    assert body == {"public_key": "pub-key", "enabled": True}


@pytest.mark.asyncio
async def test_subscribing_requires_authentication(client: AsyncClient) -> None:
    assert (await client.put("/api/v1/me/push/subscription", json={})).status_code == 401


@pytest.mark.asyncio
async def test_subscribing_is_refused_when_push_is_unconfigured(
    client: AsyncClient, user_headers
) -> None:
    # Accepting a subscription we could never deliver to would leave the UI
    # showing "on" while silently dropping every alert.
    response = await client.put(
        "/api/v1/me/push/subscription",
        json={
            "endpoint": "https://fcm.googleapis.com/fcm/send/abc",
            "keys": {"p256dh": "k", "auth": "a"},
        },
        headers=user_headers,
    )
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_subscribing_stores_the_subscription_and_enables_push(
    client: AsyncClient, user_headers, monkeypatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "vapid_public_key", "pub", raising=False)
    monkeypatch.setattr(settings, "vapid_private_key", "priv", raising=False)

    response = await client.put(
        "/api/v1/me/push/subscription",
        json={
            "endpoint": "https://fcm.googleapis.com/fcm/send/abc",
            "keys": {"p256dh": "k", "auth": "a"},
        },
        headers=user_headers,
    )
    assert response.status_code == 200
    assert response.json() == {"push_enabled": True, "has_subscription": True}

    # Only the boolean summary comes back — the raw subscription is a long
    # JSON blob and a credential for the push service, and the UI never needs it.
    assert list(response.json()) == ["push_enabled", "has_subscription"]


@pytest.mark.asyncio
async def test_subscription_without_keys_is_rejected(
    client: AsyncClient, user_headers, monkeypatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "vapid_public_key", "pub", raising=False)
    monkeypatch.setattr(settings, "vapid_private_key", "priv", raising=False)

    response = await client.put(
        "/api/v1/me/push/subscription",
        json={"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "k"}},
        headers=user_headers,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_subscription_with_an_empty_endpoint_is_rejected(
    client: AsyncClient, user_headers, monkeypatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "vapid_public_key", "pub", raising=False)
    monkeypatch.setattr(settings, "vapid_private_key", "priv", raising=False)

    response = await client.put(
        "/api/v1/me/push/subscription",
        json={"endpoint": "", "keys": {"p256dh": "k", "auth": "a"}},
        headers=user_headers,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_unsubscribing_clears_the_stored_subscription(
    client: AsyncClient, user_headers, monkeypatch
) -> None:
    # The server-side counterpart of pushManager.unsubscribe(). Clearing only in
    # the browser leaves the server sending to an endpoint nobody listens to.
    settings = get_settings()
    monkeypatch.setattr(settings, "vapid_public_key", "pub", raising=False)
    monkeypatch.setattr(settings, "vapid_private_key", "priv", raising=False)

    await client.put(
        "/api/v1/me/push/subscription",
        json={
            "endpoint": "https://fcm.googleapis.com/fcm/send/abc",
            "keys": {"p256dh": "k", "auth": "a"},
        },
        headers=user_headers,
    )
    assert (
        await client.delete("/api/v1/me/push/subscription", headers=user_headers)
    ).status_code == 204

    from app.core.security import decode_token  # noqa: F401

    assert (await client.get("/api/v1/me/notification-preferences", headers=user_headers)).json()[
        "push_enabled"
    ] is False


@pytest.mark.asyncio
async def test_unsubscribing_requires_authentication(client: AsyncClient) -> None:
    assert (await client.delete("/api/v1/me/push/subscription")).status_code == 401


async def body_of(client: AsyncClient) -> str:
    """The raw exposition text, read over HTTP like a real scraper would."""
    return (await client.get("/metrics")).text


async def _sample_count(client: AsyncClient, route: str, status: str) -> float:
    """Read one metric sample value out of the exposition text.

    Going through the HTTP endpoint rather than the registry keeps the test
    honest about label ordering, which is what actually breaks when someone adds
    a label.
    """
    match = re.search(
        rf'couponbase_http_requests_total\{{method="GET",route="{re.escape(route)}",'
        rf'status="{status}"\}} ([0-9.]+)',
        await body_of(client),
    )
    return float(match.group(1)) if match else 0.0


# ---- Backoff on the readiness check ----
#
# The durable history and the in-process backoff answer two different questions,
# and a health endpoint that only shows the first one is misleading in the
# direction that matters: "last run failed" reads like the job is still hammering
# away at the broken thing, when it is in fact paused.


@pytest.fixture(autouse=True)
def _clear_backoff_state():
    """Backoff state is module-level on the scheduler; reset it around each test.

    Without this a job left failing by one test makes the next test's readiness
    body report a pause that has nothing to do with what it seeded.
    """
    scheduler._failure_streak.clear()
    scheduler._backoff_until.clear()
    yield
    scheduler._failure_streak.clear()
    scheduler._backoff_until.clear()


@pytest.mark.asyncio
async def test_deep_health_reports_a_backing_off_job(
    client: AsyncClient, monkeypatch, async_session_maker
) -> None:
    async with async_session_maker() as db:
        db.add(
            JobRun(
                job="send_alerts",
                status=JobStatus.failed,
                started_at=datetime.now(UTC) - timedelta(minutes=5),
                detail="ConnectionError: SMTP unreachable",
            )
        )
        await db.commit()

    monkeypatch.setitem(scheduler._failure_streak, "send_alerts", 2)
    monkeypatch.setitem(scheduler._backoff_until, "send_alerts", float("inf"))

    body = (await client.get("/health/deep")).json()
    check = body["checks"]["send_alerts"]
    assert check["backing_off"] is True
    # The original failure is still reported — a pause is an addition to the
    # diagnosis, not a replacement for it.
    assert check["detail"] == "ConnectionError: SMTP unreachable"
    # And it is not "dead-lettered" at two failures.
    assert "dead_lettered" not in check


@pytest.mark.asyncio
async def test_deep_health_reports_a_dead_lettered_job(
    client: AsyncClient, monkeypatch, async_session_maker
) -> None:
    async with async_session_maker() as db:
        db.add(
            JobRun(
                job="refresh_prices",
                status=JobStatus.failed,
                started_at=datetime.now(UTC) - timedelta(hours=1),
                detail="OperationalError: could not connect",
            )
        )
        await db.commit()

    monkeypatch.setitem(scheduler._failure_streak, "refresh_prices", scheduler.DEAD_LETTER_AFTER)
    monkeypatch.setitem(scheduler._backoff_until, "refresh_prices", float("inf"))

    check = (await client.get("/health/deep")).json()["checks"]["refresh_prices"]
    assert check["dead_lettered"] is True
    assert check["consecutive_failures"] == scheduler.DEAD_LETTER_AFTER


@pytest.mark.asyncio
async def test_a_paused_job_does_not_make_readiness_fail(client: AsyncClient, monkeypatch) -> None:
    # A dead-lettered job is a real problem, but it is not a reason to stop
    # routing traffic: the API serves coupons perfectly well while ingestion is
    # broken, and a 503 here would take the whole site down over a background
    # sweep. The problem is reported; the instance stays in rotation.
    monkeypatch.setitem(
        scheduler._failure_streak, "verify_coupons", scheduler.DEAD_LETTER_AFTER + 3
    )
    monkeypatch.setitem(scheduler._backoff_until, "verify_coupons", float("inf"))

    resp = await client.get("/health/deep")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_a_healthy_job_reports_no_backoff_fields(client: AsyncClient) -> None:
    # Absent rather than false/absent-noise: a permanently-present
    # `"backing_off": false` is one more thing to scan past on every check.
    check = (await client.get("/health/deep")).json()["checks"]["expire_coupons"]
    assert "backing_off" not in check
    assert "dead_lettered" not in check
