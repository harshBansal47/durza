import uuid
from datetime import UTC, datetime, timedelta

import pytest

URL = "/api/v1/personalization"


@pytest.mark.asyncio
async def test_personalization_disabled_returns_404(client):
    resp = await client.post(f"{URL}/compute", json={"consent": True, "signals": []})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_no_personalization_consent_returns_403(client, monkeypatch):
    monkeypatch.setenv("PERSONALIZATION_ENABLED", "true")
    from app.core.config import get_settings
    get_settings.cache_clear()
    resp = await client.post(f"{URL}/compute", json={"consent": False, "signals": []})
    assert resp.status_code == 403
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_compute_no_store_header(client, monkeypatch):
    monkeypatch.setenv("PERSONALIZATION_ENABLED", "true")
    from app.core.config import get_settings
    get_settings.cache_clear()
    resp = await client.post(f"{URL}/compute", json={"consent": True, "signals": []})
    assert "no-store" in resp.headers.get("cache-control", "")
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_allowlist_rejects_sensitive_category(client, monkeypatch):
    monkeypatch.setenv("PERSONALIZATION_ENABLED", "true")
    monkeypatch.setenv("PERSONALIZATION_ALLOWED_CATEGORIES", "electronics")
    from app.core.config import get_settings
    get_settings.cache_clear()
    resp = await client.post(
        f"{URL}/compute",
        json={"consent": True, "signals": [{"interest_type": "category", "interest_key": "health", "occurred_at": datetime.now(UTC).isoformat()}]},
    )
    assert resp.json()["scores"] == {}
    get_settings.cache_clear()
