"""Price/Deal intelligence layer: recording points, rolling lows, drops, effective price."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from app.models.category import Category
from app.models.store import Store

pytestmark = pytest.mark.asyncio


async def _make_store_and_category(async_session_maker):
    async with async_session_maker() as db:
        store = Store(name="Myntra", slug="myntra")
        cat = Category(name="Fashion", slug="fashion")
        db.add_all([store, cat])
        await db.commit()
        await db.refresh(store)
        await db.refresh(cat)
        return store, cat


async def test_record_prices_and_rolling_lows(
    client: AsyncClient, admin_headers, async_session_maker
):
    store, cat = await _make_store_and_category(async_session_maker)
    resp = await client.post(
        "/api/v1/products",
        json={"name": "Kurta", "store_id": str(store.id), "category_id": str(cat.id)},
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    product_id = resp.json()["id"]

    now = datetime.now(UTC)
    for i, price in enumerate([8999, 8499, 7499, 9399]):
        resp = await client.post(
            f"/api/v1/products/{product_id}/price-points",
            json={"price": price, "captured_at": (now - timedelta(days=3 - i)).isoformat()},
            headers=admin_headers,
        )
        assert resp.status_code == 201, resp.text

    resp = await client.get(f"/api/v1/products/{product_id}")
    data = resp.json()
    assert data["current_price"] == 9399
    assert data["lowest_price_7d"] == 7499
    assert data["lowest_price_30d"] == 7499
    # The rise to 9399 was preceded by a drop to 7499 — the drop should be recorded.
    assert data["last_price_drop_pct"] is not None

    history = (await client.get(f"/api/v1/products/{product_id}/price-history")).json()
    assert len(history) == 4
    assert [h["price"] for h in history] == [9399, 7499, 8499, 8999]  # newest first


async def test_price_drop_detection(client: AsyncClient, admin_headers, async_session_maker):
    store, cat = await _make_store_and_category(async_session_maker)
    product_id = (
        await client.post(
            "/api/v1/products",
            json={"name": "Shoes", "store_id": str(store.id), "category_id": str(cat.id)},
            headers=admin_headers,
        )
    ).json()["id"]

    await client.post(
        f"/api/v1/products/{product_id}/price-points",
        json={"price": 1000},
        headers=admin_headers,
    )
    await client.post(
        f"/api/v1/products/{product_id}/price-points",
        json={"price": 800},
        headers=admin_headers,
    )
    data = (await client.get(f"/api/v1/products/{product_id}")).json()
    assert data["last_price_drop_pct"] == 20.0
    assert data["last_price_drop_at"] is not None


async def test_effective_price_includes_coupon(
    client: AsyncClient, admin_headers, async_session_maker
):
    store, cat = await _make_store_and_category(async_session_maker)
    coupon_resp = await client.post(
        "/api/v1/coupons",
        json={
            "title": "10% off everything",
            "code": "TEN",
            "discount_type": "percentage",
            "discount_value": 10,
            "store_id": str(store.id),
            "category_id": str(cat.id),
            "destination_url": "https://www.myntra.com/offer",
        },
        headers=admin_headers,
    )
    assert coupon_resp.status_code == 201, coupon_resp.text
    coupon_id = coupon_resp.json()["id"]

    product_id = (
        await client.post(
            "/api/v1/products",
            json={"name": "Jeans", "store_id": str(store.id), "category_id": str(cat.id)},
            headers=admin_headers,
        )
    ).json()["id"]
    await client.post(
        f"/api/v1/products/{product_id}/price-points",
        json={"price": 8499, "shipping": 100, "coupon_id": coupon_id},
        headers=admin_headers,
    )
    data = (await client.get(f"/api/v1/products/{product_id}")).json()
    # (8499 + 100) * 0.9 = 7739.1
    assert data["effective_price"] == 7739.1


async def test_public_cannot_record_prices(client: AsyncClient, async_session_maker):
    store, cat = await _make_store_and_category(async_session_maker)
    resp = await client.post(
        "/api/v1/products",
        json={"name": "Shirt", "store_id": str(store.id), "category_id": str(cat.id)},
    )
    assert resp.status_code in (401, 403)


async def test_price_point_requires_real_coupon(
    client: AsyncClient, admin_headers, async_session_maker
):
    store, cat = await _make_store_and_category(async_session_maker)
    product_id = (
        await client.post(
            "/api/v1/products",
            json={"name": "Hat", "store_id": str(store.id), "category_id": str(cat.id)},
            headers=admin_headers,
        )
    ).json()["id"]
    resp = await client.post(
        f"/api/v1/products/{product_id}/price-points",
        json={"price": 500, "coupon_id": str(uuid.uuid4())},
        headers=admin_headers,
    )
    assert resp.status_code == 404


async def test_product_autocomplete(client: AsyncClient, admin_headers, async_session_maker):
    store, cat = await _make_store_and_category(async_session_maker)

    for name in ("Kurta Red", "Kurta Blue", "Jeans Black", "Shoes White"):
        await client.post(
            "/api/v1/products",
            json={"name": name, "store_id": str(store.id), "category_id": str(cat.id)},
            headers=admin_headers,
        )

    hits = await client.get("/api/v1/products/autocomplete", params={"q": "kurta"})
    assert hits.status_code == 200
    data = hits.json()
    assert isinstance(data, list)
    assert "Kurta Red" in data
    assert "Kurta Blue" in data
    assert "Jeans Black" not in data


async def test_product_autocomplete_limit(client: AsyncClient, admin_headers, async_session_maker):
    store, cat = await _make_store_and_category(async_session_maker)

    for i in range(15):
        await client.post(
            "/api/v1/products",
            json={"name": f"Product {i}", "store_id": str(store.id), "category_id": str(cat.id)},
            headers=admin_headers,
        )

    hits = await client.get("/api/v1/products/autocomplete", params={"q": "product", "limit": 5})
    assert hits.status_code == 200
    data = hits.json()
    assert len(data) == 5
