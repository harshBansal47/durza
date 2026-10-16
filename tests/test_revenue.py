"""Phase 5 — Revenue tests.

Tests for the AffiliateRevenue and RevenueEvent models.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.models.affiliate_revenue import AffiliateRevenue, RevenueStatus
from app.models.revenue_event import RevenueEvent, RevenueSourceType
from app.models.store import Store

# ------------------------------------------------------------------ helpers


async def _make_store(async_session_maker, name="Test Store") -> Store:
    async with async_session_maker() as db:
        store = Store(
            name=name,
            slug=name.lower().replace(" ", "-") + f"-{uuid.uuid4().hex[:6]}",
            normalized_name=name.lower(),
            normalized_domain=f"{name.lower().replace(' ', '')}.com",
        )
        db.add(store)
        await db.commit()
        await db.refresh(store)
        return store


# ------------------------------------------------------------------ affiliate revenue


async def test_create_affiliate_revenue(async_session_maker):
    store = await _make_store(async_session_maker)

    async with async_session_maker() as db:
        revenue = AffiliateRevenue(
            merchant_id=store.id,
            gross_commission=10.0,
            adjustments=0.0,
            net_commission=10.0,
            currency="USD",
            status=RevenueStatus.pending,
            conversion_date=datetime.now(UTC),
            reported_date=datetime.now(UTC),
        )
        db.add(revenue)
        await db.commit()
        await db.refresh(revenue)

        assert revenue.id is not None
        assert revenue.merchant_id == store.id
        assert float(revenue.gross_commission) == 10.0
        assert float(revenue.net_commission) == 10.0
        assert revenue.currency == "USD"
        assert revenue.status == RevenueStatus.pending


async def test_revenue_status_transitions(async_session_maker):
    store = await _make_store(async_session_maker)

    async with async_session_maker() as db:
        revenue = AffiliateRevenue(
            merchant_id=store.id,
            gross_commission=10.0,
            net_commission=10.0,
            currency="USD",
            status=RevenueStatus.pending,
        )
        db.add(revenue)
        await db.commit()
        await db.refresh(revenue)

        # Pending → Confirmed
        revenue.status = RevenueStatus.confirmed
        revenue.confirmed_date = datetime.now(UTC)
        await db.commit()
        assert revenue.status == RevenueStatus.confirmed

        # Confirmed → Reversed
        revenue.status = RevenueStatus.reversed
        revenue.notes = "Customer refunded"
        await db.commit()
        assert revenue.status == RevenueStatus.reversed


async def test_revenue_with_adjustments(async_session_maker):
    store = await _make_store(async_session_maker)

    async with async_session_maker() as db:
        revenue = AffiliateRevenue(
            merchant_id=store.id,
            gross_commission=10.0,
            adjustments=-2.0,
            net_commission=8.0,
            currency="USD",
        )
        db.add(revenue)
        await db.commit()
        await db.refresh(revenue)

        assert float(revenue.gross_commission) == 10.0
        assert float(revenue.adjustments) == -2.0
        assert float(revenue.net_commission) == 8.0


async def test_revenue_with_normalized_currency(async_session_maker):
    store = await _make_store(async_session_maker)

    async with async_session_maker() as db:
        revenue = AffiliateRevenue(
            merchant_id=store.id,
            gross_commission=1000.0,
            net_commission=1000.0,
            currency="INR",
            normalized_amount=12.0,
            normalized_currency="USD",
            exchange_rate=83.33,
            exchange_rate_timestamp=datetime.now(UTC),
        )
        db.add(revenue)
        await db.commit()
        await db.refresh(revenue)

        assert revenue.currency == "INR"
        assert float(revenue.normalized_amount) == 12.0
        assert revenue.normalized_currency == "USD"
        assert float(revenue.exchange_rate) == 83.33


async def test_revenue_enum_validation():
    assert RevenueStatus("pending") == RevenueStatus.pending
    assert RevenueStatus("confirmed") == RevenueStatus.confirmed
    assert RevenueStatus("reversed") == RevenueStatus.reversed


async def test_revenue_cascade_delete_store(async_session_maker):
    """Deleting a store should SET NULL on revenue, not delete it."""
    store = await _make_store(async_session_maker)

    async with async_session_maker() as db:
        revenue = AffiliateRevenue(
            merchant_id=store.id,
            gross_commission=10.0,
            net_commission=10.0,
            currency="USD",
        )
        db.add(revenue)
        await db.commit()
        revenue_id = revenue.id

    # Delete the store
    async with async_session_maker() as db:
        store_obj = await db.get(Store, store.id)
        await db.delete(store_obj)
        await db.commit()

    # Revenue should still exist with NULL merchant_id
    async with async_session_maker() as db:
        result = await db.execute(select(AffiliateRevenue).where(AffiliateRevenue.id == revenue_id))
        rev = result.scalar_one_or_none()
        assert rev is not None
        assert rev.merchant_id is None


# ------------------------------------------------------------------ revenue events


async def test_create_revenue_event(async_session_maker):
    store = await _make_store(async_session_maker)

    async with async_session_maker() as db:
        event = RevenueEvent(
            source_type=RevenueSourceType.affiliate,
            source_id="conv-123",
            merchant_id=store.id,
            amount=10.0,
            currency="USD",
            status="pending",
            event_date=datetime.now(UTC),
        )
        db.add(event)
        await db.commit()
        await db.refresh(event)

        assert event.id is not None
        assert event.source_type == RevenueSourceType.affiliate
        assert event.source_id == "conv-123"
        assert float(event.amount) == 10.0
        assert event.currency == "USD"


async def test_revenue_event_source_types(async_session_maker):
    store = await _make_store(async_session_maker)

    async with async_session_maker() as db:
        events = [
            RevenueEvent(
                source_type=RevenueSourceType.affiliate,
                merchant_id=store.id,
                amount=10.0,
                currency="USD",
            ),
            RevenueEvent(
                source_type=RevenueSourceType.sponsored_placement,
                merchant_id=store.id,
                amount=500.0,
                currency="USD",
            ),
            RevenueEvent(
                source_type=RevenueSourceType.merchant_subscription,
                merchant_id=store.id,
                amount=1000.0,
                currency="USD",
            ),
            RevenueEvent(
                source_type=RevenueSourceType.direct_campaign,
                merchant_id=store.id,
                amount=2000.0,
                currency="USD",
            ),
            RevenueEvent(
                source_type=RevenueSourceType.other,
                merchant_id=store.id,
                amount=50.0,
                currency="USD",
            ),
        ]
        db.add_all(events)
        await db.commit()

        for e in events:
            await db.refresh(e)

        assert len(events) == 5
        source_types = {e.source_type for e in events}
        assert source_types == {
            RevenueSourceType.affiliate,
            RevenueSourceType.sponsored_placement,
            RevenueSourceType.merchant_subscription,
            RevenueSourceType.direct_campaign,
            RevenueSourceType.other,
        }


async def test_revenue_event_enum_validation():
    assert RevenueSourceType("affiliate") == RevenueSourceType.affiliate
    assert RevenueSourceType("sponsored_placement") == RevenueSourceType.sponsored_placement
    assert RevenueSourceType("merchant_subscription") == RevenueSourceType.merchant_subscription
    assert RevenueSourceType("direct_campaign") == RevenueSourceType.direct_campaign
    assert RevenueSourceType("other") == RevenueSourceType.other


async def test_revenue_event_cascade_delete_store(async_session_maker):
    """Deleting a store should SET NULL on revenue events, not delete them."""
    store = await _make_store(async_session_maker)

    async with async_session_maker() as db:
        event = RevenueEvent(
            source_type=RevenueSourceType.affiliate,
            merchant_id=store.id,
            amount=10.0,
            currency="USD",
        )
        db.add(event)
        await db.commit()
        event_id = event.id

    # Delete the store
    async with async_session_maker() as db:
        store_obj = await db.get(Store, store.id)
        await db.delete(store_obj)
        await db.commit()

    # Revenue event should still exist with NULL merchant_id
    async with async_session_maker() as db:
        result = await db.execute(select(RevenueEvent).where(RevenueEvent.id == event_id))
        ev = result.scalar_one_or_none()
        assert ev is not None
        assert ev.merchant_id is None
