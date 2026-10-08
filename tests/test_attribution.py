"""Phase 4 — Attribution tests.

Tests for the attribution model and service that links clicks to conversions.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models.attribution import Attribution, AttributionModel
from app.models.click import ClickEvent
from app.models.conversion import Conversion, ConversionStatus
from app.models.store import Store
from app.services.attribution import (
    create_attribution,
    find_attribution_by_click,
    get_attribution_window_days,
    is_within_attribution_window,
    record_conversion_attribution,
)

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


async def _make_click(async_session_maker, store_id, clickref=None) -> ClickEvent:
    async with async_session_maker() as db:
        click = ClickEvent(
            clickref=clickref or uuid.uuid4().hex[:32],
            store_id=store_id,
            src="coupon-page",
            network="awin",
            visitor_hash="a" * 64,
            is_bot=False,
        )
        db.add(click)
        await db.commit()
        await db.refresh(click)
        return click


async def _make_conversion(async_session_maker, clickref, network="awin") -> Conversion:
    async with async_session_maker() as db:
        conversion = Conversion(
            network=network,
            network_txn_id=f"txn-{uuid.uuid4().hex[:16]}",
            clickref=clickref,
            status=ConversionStatus.approved,
            sale_amount=100.0,
            commission_amount=5.0,
            currency="USD",
            transaction_at=datetime.now(UTC),
        )
        db.add(conversion)
        await db.commit()
        await db.refresh(conversion)
        return conversion


# ------------------------------------------------------------------ attribution model


async def test_create_attribution(async_session_maker):
    store = await _make_store(async_session_maker)
    click = await _make_click(async_session_maker, store.id)

    async with async_session_maker() as db:
        attribution = Attribution(
            visitor_id="a" * 64,
            session_id="sess-123",
            merchant_id=store.id,
            click_id=click.clickref,
            click_event_id=click.id,
            attribution_model=AttributionModel.click_touch,
            first_touch_at=datetime.now(UTC),
            last_touch_at=datetime.now(UTC),
            attribution_window=30,
        )
        db.add(attribution)
        await db.commit()
        await db.refresh(attribution)

        assert attribution.id is not None
        assert attribution.visitor_id == "a" * 64
        assert attribution.click_id == click.clickref
        assert attribution.attribution_model == AttributionModel.click_touch
        assert attribution.attribution_window == 30


async def test_attribution_enum_validation():
    assert AttributionModel("click_touch") == AttributionModel.click_touch
    assert AttributionModel("first_touch") == AttributionModel.first_touch
    assert AttributionModel("last_touch") == AttributionModel.last_touch
    assert AttributionModel("linear") == AttributionModel.linear


async def test_attribution_cascade_delete_click(async_session_maker):
    """Deleting a click should SET NULL on attribution, not delete it."""
    store = await _make_store(async_session_maker)
    click = await _make_click(async_session_maker, store.id)

    async with async_session_maker() as db:
        attribution = Attribution(
            visitor_id="a" * 64,
            click_id=click.clickref,
            click_event_id=click.id,
        )
        db.add(attribution)
        await db.commit()
        attribution_id = attribution.id

    # Delete the click
    async with async_session_maker() as db:
        click_obj = await db.get(ClickEvent, click.id)
        await db.delete(click_obj)
        await db.commit()

    # Attribution should still exist with NULL click_event_id
    async with async_session_maker() as db:
        result = await db.execute(
            select(Attribution).where(Attribution.id == attribution_id)
        )
        attr = result.scalar_one_or_none()
        assert attr is not None
        assert attr.click_event_id is None


# ------------------------------------------------------------------ attribution service


async def test_create_attribution_service(async_session_maker):
    store = await _make_store(async_session_maker)
    click = await _make_click(async_session_maker, store.id)

    async with async_session_maker() as db:
        attribution = await create_attribution(
            db,
            click=click,
            visitor_id="a" * 64,
            session_id="sess-123",
        )

        assert attribution.id is not None
        assert attribution.click_id == click.clickref
        assert attribution.visitor_id == "a" * 64
        assert attribution.attribution_model == AttributionModel.click_touch


async def test_find_attribution_by_click(async_session_maker):
    store = await _make_store(async_session_maker)
    click = await _make_click(async_session_maker, store.id)

    async with async_session_maker() as db:
        attribution = await create_attribution(
            db,
            click=click,
            visitor_id="a" * 64,
        )

        found = await find_attribution_by_click(db, click.clickref)
        assert found is not None
        assert found.id == attribution.id


async def test_record_conversion_attribution(async_session_maker):
    store = await _make_store(async_session_maker)
    click = await _make_click(async_session_maker, store.id)
    conversion = await _make_conversion(async_session_maker, click.clickref)

    async with async_session_maker() as db:
        attribution = await record_conversion_attribution(db, conversion)

        assert attribution is not None
        assert attribution.click_id == click.clickref
        assert attribution.converted_at is not None
        assert attribution.coupon_id == click.coupon_id
        assert attribution.merchant_id == click.store_id


async def test_record_conversion_attribution_no_click(async_session_maker):
    """When no matching click exists, attribution should be None."""
    conversion = await _make_conversion(async_session_maker, "nonexistent-clickref")

    async with async_session_maker() as db:
        attribution = await record_conversion_attribution(db, conversion)
        assert attribution is None


async def test_record_conversion_attribution_updates_existing(async_session_maker):
    """When attribution already exists, it should be updated, not duplicated."""
    store = await _make_store(async_session_maker)
    click = await _make_click(async_session_maker, store.id)

    async with async_session_maker() as db:
        # Create initial attribution
        attribution = await create_attribution(
            db,
            click=click,
            visitor_id="a" * 64,
        )
        attribution_id = attribution.id

    # Now record a conversion
    conversion = await _make_conversion(async_session_maker, click.clickref)

    async with async_session_maker() as db:
        updated = await record_conversion_attribution(db, conversion)

        assert updated is not None
        assert updated.id == attribution_id  # Same record, not a new one
        assert updated.converted_at is not None


async def test_is_within_attribution_window():
    now = datetime.now(UTC)
    attribution = Attribution(
        visitor_id="a" * 64,
        last_touch_at=now - timedelta(days=10),
        attribution_window=30,
    )
    assert is_within_attribution_window(attribution, now) is True


async def test_is_outside_attribution_window():
    now = datetime.now(UTC)
    attribution = Attribution(
        visitor_id="a" * 64,
        last_touch_at=now - timedelta(days=60),
        attribution_window=30,
    )
    assert is_within_attribution_window(attribution, now) is False


async def test_is_within_attribution_window_edge_case():
    """Conversion exactly at the window boundary should be valid."""
    now = datetime.now(UTC)
    attribution = Attribution(
        visitor_id="a" * 64,
        last_touch_at=now - timedelta(days=30),
        attribution_window=30,
    )
    # Exactly at the boundary — should be valid
    assert is_within_attribution_window(attribution, now) is True


async def test_get_attribution_window_days_default(async_session_maker):
    """When program doesn't exist, default to 30 days."""
    async with async_session_maker() as db:
        window = await get_attribution_window_days(db, str(uuid.uuid4()))
    assert window == 30
