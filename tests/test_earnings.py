"""EPC endpoint: clicks, confirmed commission, EPC by store and by source."""

import uuid
from datetime import UTC, datetime

from app.models.ad import Ad, AdPosition
from app.models.affiliate_revenue import AffiliateRevenue, RevenueStatus
from app.models.click import ClickEvent
from app.models.conversion import Conversion, ConversionStatus
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
    assert body["by_store"] == []
    assert body["by_source"] == []
