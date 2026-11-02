"""Account save endpoints and email notification preferences."""

import uuid

import pytest
from httpx import AsyncClient

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


async def _store_and_category(async_session_maker):
    async with async_session_maker() as db:
        store = Store(name="Myntra", slug=f"myntra-{uuid.uuid4().hex[:6]}")
        cat = Category(
            name=f"Fashion-{uuid.uuid4().hex[:6]}", slug=f"fashion-{uuid.uuid4().hex[:6]}"
        )
        db.add_all([store, cat])
        await db.commit()
        await db.refresh(store)
        await db.refresh(cat)
    return store, cat


async def test_saved_stores_and_coupons(client: AsyncClient, admin_headers, async_session_maker):
    store, cat = await _store_and_category(async_session_maker)
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
    assert (
        await client.post(f"/api/v1/me/saved-coupons/{coupon_id}", headers=headers)
    ).status_code == 201
    assert (
        await client.post(f"/api/v1/me/saved-coupons/{coupon_id}", headers=headers)
    ).status_code == 201
    saved_coupons = (await client.get("/api/v1/me/saved-coupons", headers=headers)).json()
    assert len(saved_coupons) == 1 and saved_coupons[0]["item_id"] == coupon_id

    await client.delete(f"/api/v1/me/saved-stores/{store.id}", headers=headers)
    assert (await client.get("/api/v1/me/saved-stores", headers=headers)).json() == []


async def test_notification_preferences_roundtrip(client: AsyncClient, async_session_maker):
    headers = await _user_headers(async_session_maker)
    resp = await client.get("/api/v1/me/notification-preferences", headers=headers)
    assert resp.json()["email_enabled"] is True

    resp = await client.patch(
        "/api/v1/me/notification-preferences",
        json={"email_enabled": False},
        headers=headers,
    )
    assert resp.json()["email_enabled"] is False
    assert "telegram_enabled" not in resp.json()
    assert "telegram_chat_id" not in resp.json()


async def test_me_requires_auth(client: AsyncClient):
    assert (await client.get("/api/v1/me/tracked-products")).status_code == 404
