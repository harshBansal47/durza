"""EPC endpoint: clicks, confirmed commission, EPC by store and by source."""

import uuid
from datetime import UTC, datetime

from app.models.ad import Ad, AdPosition
from app.models.affiliate_revenue import AffiliateRevenue, RevenueStatus
from app.models.category import Category
from app.models.click import ClickEvent
from app.models.conversion import Conversion, ConversionStatus
from app.models.coupon import Coupon, DiscountType
from app.models.store import Store


async def _seed(db, store_id=None):
    store = Store(
        name="Acme",
        slug=f"acme-{uuid.uuid4().hex[:6]}",
        normalized_name="acme",
        normalized_domain="acme.example",
    )
    db.add(store)
    await db.flush()
    click = ClickEvent(
        clickref=uuid.uuid4().hex[:32], src="coupon-page", network="awin", store_id=store.id
    )
    db.add(click)
    await db.flush()
    conversion = Conversion(
        network="awin",
        network_txn_id=uuid.uuid4().hex[:8],
        clickref=click.clickref,
        click_id=click.id,
        store_id=store.id,
        status=ConversionStatus.approved,
        sale_amount=1000,
        commission_amount=40,
        currency="INR",
        transaction_at=datetime.now(UTC),
    )
    db.add(conversion)
    await db.flush()
    revenue = AffiliateRevenue(
        conversion_id=conversion.id,
        merchant_id=store.id,
        gross_commission=40,
        net_commission=40,
        currency="INR",
        status=RevenueStatus.confirmed,
        conversion_date=datetime.now(UTC),
    )
    db.add(revenue)
    await db.commit()
    return store, click, conversion, revenue


async def test_epc_requires_admin(client):
    response = await client.get("/api/v1/earnings/epc")
    assert response.status_code == 401


async def test_epc_by_store_and_source(client, async_session_maker, admin_headers):
    async with async_session_maker() as db:
        await _seed(db)

    response = await client.get("/api/v1/earnings/epc?days=30", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()

    assert body["by_store"], body
    row = body["by_store"][0]
    assert row["store_name"] == "Acme"
    assert row["clicks"] == 1
    assert row["conversions"] == 1
    assert row["commission"] == 40.0
    assert row["epc"] == 40.0
    assert row["currency"] == "INR"

    assert body["by_source"], body
    src_row = body["by_source"][0]
    assert src_row["source"] == "coupon-page"
    assert src_row["clicks"] == 1
    assert src_row["epc"] == 40.0


async def test_epc_excludes_ad_clicks_and_pending_revenue(
    client, async_session_maker, admin_headers
):
    async with async_session_maker() as db:
        store, _click, _conversion, revenue = await _seed(db)
        revenue.status = RevenueStatus.pending
        ad = Ad(
            position=AdPosition.header,
            image_url="https://x.example/a.png",
            target_url="https://x.example",
        )
        db.add(ad)
        await db.flush()
        ad_click = ClickEvent(
            clickref=uuid.uuid4().hex[:32],
            src="coupon-page",
            network="awin",
            store_id=store.id,
            ad_id=ad.id,
        )
        db.add(ad_click)
        await db.commit()

    response = await client.get("/api/v1/earnings/epc?days=30", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    # Pending revenue must not count, and the sponsored click must not pad the base.
    assert body["by_store"][0]["clicks"] == 1
    assert body["by_store"][0]["commission"] == 0.0
    assert body["by_store"][0]["currency"] is None
    assert body["by_source"][0]["clicks"] == 1
    assert body["by_source"][0]["commission"] == 0.0
    assert body["by_source"][0]["currency"] is None


async def test_epc_includes_clicks_without_confirmed_revenue(
    client, async_session_maker, admin_headers
):
    async with async_session_maker() as db:
        store = Store(
            name="No Earnings",
            slug=f"no-earnings-{uuid.uuid4().hex[:6]}",
            normalized_name="no earnings",
            normalized_domain="no-earnings.example",
        )
        category = Category(
            name="Test",
            slug=f"test-{uuid.uuid4().hex[:6]}",
        )
        db.add_all([store, category])
        await db.flush()
        coupon = Coupon(
            title="No earning offer",
            slug=f"no-earning-{uuid.uuid4().hex[:6]}",
            discount_type=DiscountType.percentage,
            store_id=store.id,
            category_id=category.id,
            destination_url="https://merchant.example/offer",
        )
        db.add(coupon)
        await db.flush()
        db.add(
            ClickEvent(
                clickref=uuid.uuid4().hex[:32],
                src="coupon-page",
                network="awin",
                store_id=store.id,
                coupon_id=coupon.id,
            )
        )
        await db.commit()

    response = await client.get("/api/v1/earnings/epc?days=30", headers=admin_headers)

    assert response.status_code == 200
    body = response.json()
    rows = (
        next(item for item in body["by_store"] if item["store_name"] == "No Earnings"),
        next(item for item in body["by_source"] if item["source"] == "coupon-page"),
        body["by_coupon"][0],
    )
    for row in rows:
        assert row["clicks"] == 1
        assert row["conversions"] == 0
        assert row["commission"] == 0.0
        assert row["epc"] == 0.0
        assert row["currency"] is None
