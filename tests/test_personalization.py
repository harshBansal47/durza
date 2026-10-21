from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.user import User

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


@pytest.mark.asyncio
async def test_decay_is_deterministic_with_a_frozen_clock():
    from datetime import timedelta

    from app.routers.api.v1.personalization import _decay

    occurred = datetime(2026, 1, 1, tzinfo=UTC)
    assert _decay(occurred, occurred, 14) == 1.0
    assert _decay(occurred, occurred + timedelta(days=14), 14) == pytest.approx(0.5)
    assert _decay(occurred, occurred + timedelta(days=28), 14) == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_expired_interests_are_purged_on_read(async_session_maker, client):
    from datetime import timedelta

    from app.core.security import create_access_token
    from app.models.user_interest import UserInterest
    from app.schemas.user import UserCreate
    from app.services import user_service

    async with async_session_maker() as session:
        user = await user_service.create_user(
            session, UserCreate(email="retention@example.com", password="supersecret123")
        )
        user.personalization_consent = True
        session.add(
            UserInterest(
                user_id=user.id,
                interest_type="category",
                interest_key="electronics",
                score=1.0,
                expires_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        await session.commit()
        token = create_access_token(str(user.id), user.role.value)

    resp = await client.get(f"{URL}/export", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json() == []

    async with async_session_maker() as session:
        remaining = (await session.execute(select(UserInterest))).scalars().all()
        assert remaining == []


@pytest.mark.asyncio
async def test_withdrawal_deletes_rows_and_revokes_consent(async_session_maker, client):
    from app.core.security import create_access_token
    from app.models.user_interest import UserInterest
    from app.schemas.user import UserCreate
    from app.services import user_service

    async with async_session_maker() as session:
        user = await user_service.create_user(
            session, UserCreate(email="withdraw@example.com", password="supersecret123")
        )
        user.personalization_consent = True
        session.add(
            UserInterest(
                user_id=user.id,
                interest_type="category",
                interest_key="electronics",
                score=1.0,
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
        await session.commit()
        token = create_access_token(str(user.id), user.role.value)

    resp = await client.delete(f"{URL}/interests", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 204

    async with async_session_maker() as session:
        rows = (await session.execute(select(UserInterest))).scalars().all()
        assert rows == []
        refreshed = await session.get(User, user.id)
        assert refreshed is not None
        assert refreshed.personalization_consent is False


@pytest.mark.asyncio
async def test_account_deletion_cascades_interests(async_session_maker):
    from app.models.user_interest import UserInterest
    from app.schemas.user import UserCreate
    from app.services import user_service

    async with async_session_maker() as session:
        user = await user_service.create_user(
            session, UserCreate(email="cascade@example.com", password="supersecret123")
        )
        session.add(
            UserInterest(
                user_id=user.id,
                interest_type="category",
                interest_key="electronics",
                score=1.0,
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
        await session.commit()
        await user_service.delete_user(session, user)

    async with async_session_maker() as session:
        rows = (await session.execute(select(UserInterest))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_export_returns_only_own_rows_with_no_store(async_session_maker, client):
    from app.core.security import create_access_token
    from app.models.user_interest import UserInterest
    from app.schemas.user import UserCreate
    from app.services import user_service

    async with async_session_maker() as session:
        owner = await user_service.create_user(
            session, UserCreate(email="export-owner@example.com", password="supersecret123")
        )
        session.add(
            UserInterest(
                user_id=owner.id,
                interest_type="store",
                interest_key="nike",
                score=0.8,
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
        await session.commit()
        token = create_access_token(str(owner.id), owner.role.value)

    resp = await client.get(f"{URL}/export", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "private, no-store"
    body = resp.json()
    assert len(body) == 1
    assert body[0]["interest_type"] == "store"
    assert body[0]["interest_key"] == "nike"
    assert body[0]["score"] == 0.8
