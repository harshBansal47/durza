"""Retention layer: saved items, tracked products, alert evaluation, preferences."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from app.core.security import create_access_token
from app.models.category import Category
from app.models.store import Store
from app.models.user import Role
from app.schemas.user import UserCreate
from app.services import user_service

pytestmark = pytest.mark.asyncio


async def _user_headers(async_session_maker) -> dict[str, str]:
    async with async_session_maker() as db:
        email = f"u-{uuid.uuid4().hex[:8]}@example.com"
        user = await user_service.create_user(
            db, UserCreate(email=email, password="supersecret123"), role=Role.user
        )
        return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role.value)}"}


async def _store_cat_product(client, admin_headers, async_session_maker):
    async with async_session_maker() as db:
        store = Store(name="Myntra", slug=f"myntra-{uuid.uuid4().hex[:6]}")
        cat = Category(
            name=f"Fashion-{uuid.uuid4().hex[:6]}", slug=f"fashion-{uuid.uuid4().hex[:6]}"
        )
        db.add_all([store, cat])
        await db.commit()
        await db.refresh(store)
        await db.refresh(cat)
    product_id = (
        await client.post(
            "/api/v1/products",
            json={"name": "MacBook Air M4", "store_id": str(store.id), "category_id": str(cat.id)},
            headers=admin_headers,
        )
    ).json()["id"]
    return store, cat, product_id


async def test_saved_stores_and_coupons(client: AsyncClient, admin_headers, async_session_maker):
    store, _cat, _product_id = await _store_cat_product(client, admin_headers, async_session_maker)
    headers = await _user_headers(async_session_maker)

    assert (
        await client.post(f"/api/v1/me/saved-stores/{store.id}", headers=headers)
    ).status_code == 201
    # Saving twice is idempotent.
    assert (
        await client.post(f"/api/v1/me/saved-stores/{store.id}", headers=headers)
    ).status_code == 201
    saved = (await client.get("/api/v1/me/saved-stores", headers=headers)).json()
    assert len(saved) == 1 and saved[0]["item_id"] == str(store.id)

    await client.delete(f"/api/v1/me/saved-stores/{store.id}", headers=headers)
    assert (await client.get("/api/v1/me/saved-stores", headers=headers)).json() == []


async def test_track_product_and_target_update(
    client: AsyncClient, admin_headers, async_session_maker
):
    _, _, product_id = await _store_cat_product(client, admin_headers, async_session_maker)
    headers = await _user_headers(async_session_maker)

    resp = await client.post(
        "/api/v1/me/tracked-products",
        json={"product_id": product_id, "target_price": 75000},
        headers=headers,
    )
    assert resp.status_code == 201
    tracked_id = resp.json()["id"]

    resp = await client.patch(
        f"/api/v1/me/tracked-products/{tracked_id}",
        json={"target_price": 70000},
        headers=headers,
    )
    assert resp.json()["target_price"] == 70000

    tracked = (await client.get("/api/v1/me/tracked-products", headers=headers)).json()
    assert len(tracked) == 1


async def test_alert_fires_when_target_met(client: AsyncClient, admin_headers, async_session_maker):
    _, _, product_id = await _store_cat_product(client, admin_headers, async_session_maker)
    headers = await _user_headers(async_session_maker)

    await client.post(
        "/api/v1/me/tracked-products",
        json={"product_id": product_id, "target_price": 75000},
        headers=headers,
    )
    now = datetime.now(UTC)
    await client.post(
        f"/api/v1/products/{product_id}/price-points",
        json={"price": 90000, "captured_at": (now - timedelta(days=2)).isoformat()},
        headers=admin_headers,
    )
    await client.post(
        f"/api/v1/products/{product_id}/price-points",
        json={"price": 74999, "captured_at": now.isoformat()},
        headers=admin_headers,
    )

    from app.core.database import get_db  # noqa: F401
    from app.services.alert_service import run_alert_scan

    async with async_session_maker() as db:
        sent = await run_alert_scan(db)
    assert sent >= 1

    alerts = (await client.get("/api/v1/me/alerts", headers=headers)).json()
    assert any(a["kind"] == "target_met" for a in alerts)

    # Second scan on the same observation sends nothing new.
    async with async_session_maker() as db:
        sent2 = await run_alert_scan(db)
    assert sent2 == 0


async def test_coupon_appeared_alert(client: AsyncClient, admin_headers, async_session_maker):
    store, cat, product_id = await _store_cat_product(client, admin_headers, async_session_maker)
    headers = await _user_headers(async_session_maker)

    await client.post(
        "/api/v1/me/tracked-products",
        json={"product_id": product_id},  # no target -> coupon alert can fire
        headers=headers,
    )
    coupon_id = (
        await client.post(
            "/api/v1/coupons",
            json={
                "title": "1000 off",
                "code": "X1000",
                "discount_type": "fixed",
                "discount_value": 1000,
                "store_id": str(store.id),
                "category_id": str(cat.id),
                "destination_url": "https://www.myntra.com/x",
            },
            headers=admin_headers,
        )
    ).json()["id"]

    now = datetime.now(UTC)
    await client.post(
        f"/api/v1/products/{product_id}/price-points",
        json={"price": 85000, "captured_at": (now - timedelta(days=1)).isoformat()},
        headers=admin_headers,
    )
    await client.post(
        f"/api/v1/products/{product_id}/price-points",
        json={"price": 85000, "coupon_id": coupon_id, "captured_at": now.isoformat()},
        headers=admin_headers,
    )

    from app.services.alert_service import run_alert_scan

    async with async_session_maker() as db:
        await run_alert_scan(db)
    alerts = (await client.get("/api/v1/me/alerts", headers=headers)).json()
    assert any(a["kind"] == "coupon_appeared" for a in alerts)


async def test_notification_preferences_roundtrip(client: AsyncClient, async_session_maker):
    headers = await _user_headers(async_session_maker)
    resp = await client.get("/api/v1/me/notification-preferences", headers=headers)
    assert resp.json()["email_enabled"] is True

    resp = await client.patch(
        "/api/v1/me/notification-preferences",
        json={"telegram_enabled": True, "telegram_chat_id": "12345"},
        headers=headers,
    )
    assert resp.json()["telegram_enabled"] is True
    assert resp.json()["telegram_chat_id"] == "12345"


async def test_me_requires_auth(client: AsyncClient):
    assert (await client.get("/api/v1/me/tracked-products")).status_code == 401
