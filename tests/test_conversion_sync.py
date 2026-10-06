"""Conversion sync: network transaction reports -> Conversion + AffiliateRevenue."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.affiliate_revenue import AffiliateRevenue, RevenueStatus
from app.models.click import ClickEvent
from app.models.conversion import Conversion, ConversionStatus
from app.models.store import Store
from app.services.conversion_sync import (
    NetworkTransaction,
    map_status,
    sync_conversions,
    upsert_transaction,
)


def _txn(**overrides) -> NetworkTransaction:
    base = {
        "network": "awin",
        "network_txn_id": "txn-1",
        "clickref": None,
        "raw_status": "approved",
        "sale_amount": Decimal("1200.00"),
        "commission_amount": Decimal("48.00"),
        "currency": "INR",
        "transaction_at": datetime(2026, 10, 1, tzinfo=UTC),
        "validated_at": datetime(2026, 10, 3, tzinfo=UTC),
        "decline_reason": None,
    }
    base.update(overrides)
    return NetworkTransaction(**base)


class FakeClient:
    network = "awin"

    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, *, since):
        return self._rows


@pytest.mark.asyncio
async def test_map_status():
    assert map_status("pending") is ConversionStatus.pending
    assert map_status("approved") is ConversionStatus.approved
    assert map_status("Confirmed") is ConversionStatus.approved
    assert map_status("declined") is ConversionStatus.declined
    assert map_status("rejected") is ConversionStatus.declined
    assert map_status("cancelled") is ConversionStatus.deleted
    assert map_status(None) is ConversionStatus.pending
    assert map_status("weird") is ConversionStatus.pending


@pytest.mark.asyncio
async def test_upsert_inserts_conversion_and_revenue(async_session_maker):
    async with async_session_maker() as db:
        outcome = await upsert_transaction(db, _txn())
        await db.commit()
        assert outcome == "inserted"

        conversion = (await db.execute(select(Conversion))).scalars().one()
        assert conversion.status is ConversionStatus.approved
        assert conversion.network_txn_id == "txn-1"
        assert float(conversion.commission_amount) == 48.0

        revenue = (await db.execute(select(AffiliateRevenue))).scalars().one()
        assert revenue.conversion_id == conversion.id
        assert revenue.status is RevenueStatus.confirmed
        assert revenue.net_commission == 48.0
        assert revenue.currency == "INR"


@pytest.mark.asyncio
async def test_upsert_is_idempotent_and_updates_status(async_session_maker):
    async with async_session_maker() as db:
        await upsert_transaction(db, _txn(raw_status="pending"))
        await db.commit()
        outcome = await upsert_transaction(db, _txn(raw_status="declined", decline_reason="refund"))
        await db.commit()
        assert outcome == "updated"

        conversions = (await db.execute(select(Conversion))).scalars().all()
        assert len(conversions) == 1
        assert conversions[0].status is ConversionStatus.declined
        assert conversions[0].decline_reason == "refund"

        revenues = (await db.execute(select(AffiliateRevenue))).scalars().all()
        assert len(revenues) == 1
        assert revenues[0].status is RevenueStatus.reversed
        assert revenues[0].notes == "refund"


@pytest.mark.asyncio
async def test_clickref_matches_click_and_store(async_session_maker):
    async with async_session_maker() as db:
        store = Store(
            name="Acme",
            slug=f"acme-{uuid.uuid4().hex[:6]}",
            normalized_name="acme",
            normalized_domain="acme.example",
        )
        db.add(store)
        await db.flush()
        click = ClickEvent(clickref="clk123", src="coupon-page", network="awin", store_id=store.id)
        db.add(click)
        await db.commit()

        await upsert_transaction(db, _txn(clickref="clk123"))
        await db.commit()

        conversion = (await db.execute(select(Conversion))).scalars().one()
        assert conversion.click_id == click.id
        assert conversion.store_id == store.id
        assert conversion.click_at is not None


@pytest.mark.asyncio
async def test_sync_conversions_end_to_end(async_session_maker):
    rows = [
        _txn(network_txn_id="t1"),
        _txn(network_txn_id="t2", raw_status="pending", commission_amount=Decimal(10)),
    ]
    async with async_session_maker() as db:
        detail = await sync_conversions(db, clients=[FakeClient(rows)])
        assert "inserted 2" in detail

        count = len((await db.execute(select(Conversion))).scalars().all())
        assert count == 2
        statuses = {
            c.network_txn_id: c.status
            for c in (await db.execute(select(Conversion))).scalars().all()
        }
        assert statuses["t1"] is ConversionStatus.approved
        assert statuses["t2"] is ConversionStatus.pending


@pytest.mark.asyncio
async def test_sync_conversions_no_clients(async_session_maker, monkeypatch):
    from app.core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("AWIN_API_KEY", "")
    monkeypatch.setenv("CUELINKS_API_KEY", "")
    try:
        async with async_session_maker() as db:
            detail = await sync_conversions(db, clients=[])
            assert "skipped" in detail
    finally:
        get_settings.cache_clear()
