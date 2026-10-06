"""EPC service: store and coupon level, rolling 30-day window."""

import uuid
from datetime import UTC, datetime

from app.models.affiliate_revenue import AffiliateRevenue, RevenueStatus
from app.models.category import Category
from app.models.click import ClickEvent
from app.models.conversion import Conversion, ConversionStatus
from app.models.coupon import Coupon
from app.models.store import Store
from app.services.epc import epc_by_coupon, epc_by_store


async def _seed(db):
    store = Store(
        name="Acme",
        slug=f"acme-{uuid.uuid4().hex[:6]}",
        normalized_name="acme",
        normalized_domain="acme.example",
    )
    db.add(store)
    await db.flush()
    category = Category(name="Misc", slug=f"misc-{uuid.uuid4().hex[:6]}")
    db.add(category)
    await db.flush()
    coupon = Coupon(
        title="10% off",
        code="TEN",
        slug=f"ten-{uuid.uuid4().hex[:6]}",
        store_id=store.id,
        destination_url="https://acme.example/deal",
        category_id=category.id,
        discount_type="percentage",
        discount_value=10,
        clicks_count=0,
        success_count=0,
        fail_count=0,
        source_count=1,
    )
    db.add(coupon)
    await db.flush()
    click = ClickEvent(
        clickref=uuid.uuid4().hex[:32],
        src="coupon-page",
        network="awin",
        store_id=store.id,
        coupon_id=coupon.id,
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
        commission_amount=50,
        currency="INR",
        transaction_at=datetime.now(UTC),
    )
    db.add(conversion)
    await db.flush()
    db.add(
        AffiliateRevenue(
            conversion_id=conversion.id,
            merchant_id=store.id,
            gross_commission=50,
            net_commission=50,
            currency="INR",
            status=RevenueStatus.confirmed,
            conversion_date=datetime.now(UTC),
        )
    )
    await db.commit()
    return store, coupon


async def test_epc_by_store_and_coupon(async_session_maker):
    async with async_session_maker() as db:
        store, coupon = await _seed(db)
        by_store = await epc_by_store(db)
        by_coupon = await epc_by_coupon(db)
        assert by_store[store.id] == 50.0
        assert by_coupon[coupon.id] == 50.0


async def test_epc_ignores_pending_commission(async_session_maker):
    async with async_session_maker() as db:
        _store, _coupon = await _seed(db)
        # Flip the only revenue row to pending: EPC must drop to zero rather
        # than chase unconfirmed money.
        from sqlalchemy import update

        await db.execute(update(AffiliateRevenue).values(status=RevenueStatus.pending))
        await db.commit()
        # The clicks are still there; only the commission side must go to zero.
        assert all(epc == 0.0 for epc in (await epc_by_store(db)).values())
        assert all(epc == 0.0 for epc in (await epc_by_coupon(db)).values())
