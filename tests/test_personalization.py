from datetime import UTC, datetime

import pytest

URL = "/api/v1/personalization"


def _enable(monkeypatch, allowlist: str = "electronics"):
    monkeypatch.setenv("PERSONALIZATION_ENABLED", "true")
    monkeypatch.setenv("PERSONALIZATION_ALLOWED_CATEGORIES", allowlist)
    from app.core.config import get_settings

    get_settings.cache_clear()


def _clear():
    from app.core.config import get_settings

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_personalization_disabled_returns_404(client):
    resp = await client.post(f"{URL}/compute", json={"consent": True, "signals": []})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_disabled_module_returns_404_with_no_store(client):
    resp = await client.get(f"{URL}/picked-for-you")
    assert resp.status_code == 404
    assert "no-store" in resp.headers.get("cache-control", "")


@pytest.mark.asyncio
async def test_no_personalization_consent_returns_403(client, monkeypatch):
    _enable(monkeypatch)
    resp = await client.post(f"{URL}/compute", json={"consent": False, "signals": []})
    assert resp.status_code == 403
    _clear()


@pytest.mark.asyncio
async def test_compute_no_store_header(client, monkeypatch):
    _enable(monkeypatch)
    resp = await client.post(f"{URL}/compute", json={"consent": True, "signals": []})
    assert "no-store" in resp.headers.get("cache-control", "")
    _clear()


@pytest.mark.asyncio
async def test_allowlist_rejects_sensitive_categories_even_if_listed(client, monkeypatch):
    _enable(monkeypatch, allowlist="electronics,health,loans,insurance,adult")
    now = datetime.now(UTC).isoformat()
    resp = await client.post(
        f"{URL}/compute",
        json={
            "consent": True,
            "signals": [
                {"interest_type": "category", "interest_key": key, "occurred_at": now}
                for key in ("health", "loans", "insurance", "adult", "electronics")
            ],
        },
    )
    assert set(resp.json()["scores"]) == {"category:electronics"}
    _clear()


@pytest.mark.asyncio
async def test_oversized_signal_list_is_rejected(client, monkeypatch):
    _enable(monkeypatch)
    now = datetime.now(UTC).isoformat()
    signals = [
        {"interest_type": "category", "interest_key": "electronics", "occurred_at": now}
        for _ in range(51)
    ]
    resp = await client.post(f"{URL}/compute", json={"consent": True, "signals": signals})
    assert resp.status_code == 422
    _clear()


@pytest.mark.asyncio
async def test_client_score_is_ignored(client, monkeypatch):
    _enable(monkeypatch)
    now = datetime.now(UTC).isoformat()
    resp = await client.post(
        f"{URL}/compute",
        json={
            "consent": True,
            "signals": [
                {
                    "interest_type": "category",
                    "interest_key": "electronics",
                    "occurred_at": now,
                    "score": 999.0,
                }
            ],
        },
    )
    score = resp.json()["scores"]["category:electronics"]
    assert 0.99 <= score <= 1.0
    _clear()
