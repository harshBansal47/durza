"""Phase 1 — Monetization foundation tests.

Tests for the affiliate provider, program, and destination models that
form the basis of the monetization flywheel.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.affiliate_destination import AffiliateDestination, DestinationStatus
from app.models.affiliate_program import AffiliateProgram, CommissionType, ProgramStatus
from app.models.affiliate_provider import AffiliateProvider, ProviderStatus, ProviderType
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


async def _make_provider(async_session_maker, name="Test Provider", **kwargs) -> AffiliateProvider:
    async with async_session_maker() as db:
        provider = AffiliateProvider(
            name=name,
            slug=kwargs.pop("slug", name.lower().replace(" ", "-") + f"-{uuid.uuid4().hex[:6]}"),
            **kwargs,
        )
        db.add(provider)
        await db.commit()
        await db.refresh(provider)
        return provider


# ------------------------------------------------------------------ providers


async def test_create_provider(async_session_maker):
    provider = await _make_provider(async_session_maker, "Awin")
    assert provider.id is not None
    assert provider.name == "Awin"
    assert provider.slug.startswith("awin-")
    assert provider.provider_type == ProviderType.cpa
    assert provider.enabled is True
    assert provider.priority == 100
    assert provider.api_status == ProviderStatus.unconfigured
    assert provider.tracking_status == ProviderStatus.unconfigured
    assert provider.countries == []
    assert provider.subid_supported is False
    assert provider.deeplink_supported is False
    assert provider.postback_supported is False


async def test_provider_with_full_config(async_session_maker):
    provider = await _make_provider(
        async_session_maker,
        "Rakuten",
        provider_type=ProviderType.hybrid,
        priority=10,
        countries=["US", "GB"],
        api_status=ProviderStatus.active,
        tracking_status=ProviderStatus.active,
        credentials_reference="RAKUTEN_API_KEY",
        subid_supported=True,
        deeplink_supported=True,
        postback_supported=True,
        default_cookie_days=30,
        notes="Requires approval for each merchant",
    )
    assert provider.provider_type == ProviderType.hybrid
    assert provider.priority == 10
    assert provider.countries == ["US", "GB"]
    assert provider.api_status == ProviderStatus.active
    assert provider.tracking_status == ProviderStatus.active
    assert provider.credentials_reference == "RAKUTEN_API_KEY"
    assert provider.subid_supported is True
    assert provider.deeplink_supported is True
    assert provider.postback_supported is True
    assert provider.default_cookie_days == 30
    assert "Requires approval" in provider.notes


async def test_provider_slug_unique_constraint(async_session_maker):
    await _make_provider(async_session_maker, "Awin", slug="awin-test")
    # IntegrityError specifically, not `Exception`: this asserts the unique
    # constraint fired, and a bare `Exception` would also pass if the helper
    # failed for some unrelated reason — which is the one way this test could
    # have been green while the constraint was not actually enforced.
    with pytest.raises(IntegrityError):
        await _make_provider(async_session_maker, "Awin Duplicate", slug="awin-test")


async def test_provider_enum_validation():
    # Valid values should work
    assert ProviderType("cpa") == ProviderType.cpa
    assert ProviderType("cpc") == ProviderType.cpc
    assert ProviderType("hybrid") == ProviderType.hybrid
    assert ProviderStatus("active") == ProviderStatus.active
    assert ProviderStatus("degraded") == ProviderStatus.degraded
    assert ProviderStatus("paused") == ProviderStatus.paused
    assert ProviderStatus("unconfigured") == ProviderStatus.unconfigured


# ------------------------------------------------------------------ programs


async def test_create_program(async_session_maker):
    store = await _make_store(async_session_maker)
    provider = await _make_provider(async_session_maker, "Awin")

    async with async_session_maker() as db:
        program = AffiliateProgram(
            merchant_id=store.id,
            provider_id=provider.id,
            external_program_id="AWIN-12345",
            status=ProgramStatus.active,
            country="US",
            commission_type=CommissionType.percentage,
            commission_value=5.0,
            cookie_duration_days=30,
            deeplink_supported=True,
            tracking_supported=True,
        )
        db.add(program)
        await db.commit()
        await db.refresh(program)

        assert program.id is not None
        assert program.merchant_id == store.id
        assert program.provider_id == provider.id
        assert program.external_program_id == "AWIN-12345"
        assert program.status == ProgramStatus.active
        assert program.country == "US"
        assert program.commission_type == CommissionType.percentage
        assert float(program.commission_value) == 5.0
        assert program.cookie_duration_days == 30
        assert program.deeplink_supported is True
        assert program.tracking_supported is True


async def test_multiple_programs_per_merchant(async_session_maker):
    """A merchant can be enrolled in multiple providers' programs."""
    store = await _make_store(async_session_maker, "Nike")
    awin = await _make_provider(async_session_maker, "Awin")
    rakuten = await _make_provider(async_session_maker, "Rakuten")
    cj = await _make_provider(async_session_maker, "CJ")

    async with async_session_maker() as db:
        programs = [
            AffiliateProgram(
                merchant_id=store.id,
                provider_id=awin.id,
                commission_value=4.0,
                country="US",
            ),
            AffiliateProgram(
                merchant_id=store.id,
                provider_id=rakuten.id,
                commission_value=5.0,
                country="US",
            ),
            AffiliateProgram(
                merchant_id=store.id,
                provider_id=cj.id,
                commission_value=6.0,
                country="US",
            ),
        ]
        db.add_all(programs)
        await db.commit()

        for p in programs:
            await db.refresh(p)

        # All three programs should exist
        result = await db.execute(
            select(AffiliateProgram).where(AffiliateProgram.merchant_id == store.id)
        )
        found = result.scalars().all()
        assert len(found) == 3
        provider_ids = {p.provider_id for p in found}
        assert provider_ids == {awin.id, rakuten.id, cj.id}


async def test_country_specific_programs(async_session_maker):
    """A merchant can have different programs for different countries."""
    store = await _make_store(async_session_maker, "Global Store")
    provider = await _make_provider(async_session_maker, "Awin")

    async with async_session_maker() as db:
        us_program = AffiliateProgram(
            merchant_id=store.id,
            provider_id=provider.id,
            country="US",
            commission_value=5.0,
        )
        gb_program = AffiliateProgram(
            merchant_id=store.id,
            provider_id=provider.id,
            country="GB",
            commission_value=4.0,
        )
        global_program = AffiliateProgram(
            merchant_id=store.id,
            provider_id=provider.id,
            country=None,
            commission_value=3.0,
        )
        db.add_all([us_program, gb_program, global_program])
        await db.commit()

        result = await db.execute(
            select(AffiliateProgram).where(AffiliateProgram.merchant_id == store.id)
        )
        found = result.scalars().all()
        assert len(found) == 3
        countries = {p.country for p in found}
        assert countries == {"US", "GB", None}


async def test_inactive_program_status(async_session_maker):
    store = await _make_store(async_session_maker)
    provider = await _make_provider(async_session_maker, "Awin")

    async with async_session_maker() as db:
        program = AffiliateProgram(
            merchant_id=store.id,
            provider_id=provider.id,
            status=ProgramStatus.paused,
            commission_value=5.0,
        )
        db.add(program)
        await db.commit()
        await db.refresh(program)
        assert program.status == ProgramStatus.paused


async def test_program_enum_validation():
    assert CommissionType("percentage") == CommissionType.percentage
    assert CommissionType("fixed") == CommissionType.fixed
    assert CommissionType("tiered") == CommissionType.tiered
    assert ProgramStatus("active") == ProgramStatus.active
    assert ProgramStatus("pending") == ProgramStatus.pending
    assert ProgramStatus("paused") == ProgramStatus.paused
    assert ProgramStatus("terminated") == ProgramStatus.terminated


# ------------------------------------------------------------------ destinations


async def test_create_destination(async_session_maker):
    store = await _make_store(async_session_maker)
    provider = await _make_provider(async_session_maker, "Awin")

    async with async_session_maker() as db:
        program = AffiliateProgram(
            merchant_id=store.id,
            provider_id=provider.id,
            commission_value=5.0,
        )
        db.add(program)
        await db.commit()
        await db.refresh(program)

        destination = AffiliateDestination(
            merchant_id=store.id,
            program_id=program.id,
            destination_url="https://nike.com/sale",
            tracking_url="https://nike.com/sale?aff=awin",
            status=DestinationStatus.active,
            country="US",
            deep_link=False,
        )
        db.add(destination)
        await db.commit()
        await db.refresh(destination)

        assert destination.id is not None
        assert destination.merchant_id == store.id
        assert destination.program_id == program.id
        assert destination.destination_url == "https://nike.com/sale"
        assert destination.tracking_url == "https://nike.com/sale?aff=awin"
        assert destination.status == DestinationStatus.active
        assert destination.country == "US"
        assert destination.deep_link is False


async def test_destination_enum_validation():
    assert DestinationStatus("active") == DestinationStatus.active
    assert DestinationStatus("paused") == DestinationStatus.paused
    assert DestinationStatus("invalid") == DestinationStatus.invalid


# ------------------------------------------------------------------ relationships


async def test_program_relationships(async_session_maker):
    store = await _make_store(async_session_maker)
    provider = await _make_provider(async_session_maker, "Awin")

    async with async_session_maker() as db:
        program = AffiliateProgram(
            merchant_id=store.id,
            provider_id=provider.id,
            commission_value=5.0,
        )
        db.add(program)
        await db.commit()
        await db.refresh(program)

        # Test relationships load correctly (within session context)
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        result = await db.execute(
            select(AffiliateProgram)
            .where(AffiliateProgram.id == program.id)
            .options(selectinload(AffiliateProgram.merchant), selectinload(AffiliateProgram.provider))
        )
        loaded = result.scalar_one()
        assert loaded.merchant.id == store.id
        assert loaded.provider.id == provider.id
        assert loaded.merchant.name == store.name
        assert loaded.provider.name == provider.name


async def test_cascade_delete_store_removes_programs(async_session_maker):
    """Deleting a store should cascade-delete its affiliate programs."""
    store = await _make_store(async_session_maker)
    provider = await _make_provider(async_session_maker, "Awin")

    async with async_session_maker() as db:
        program = AffiliateProgram(
            merchant_id=store.id,
            provider_id=provider.id,
            commission_value=5.0,
        )
        db.add(program)
        await db.commit()
        program_id = program.id

    # Delete the store
    async with async_session_maker() as db:
        store_obj = await db.get(Store, store.id)
        await db.delete(store_obj)
        await db.commit()

    # Program should be gone
    async with async_session_maker() as db:
        result = await db.execute(select(AffiliateProgram).where(AffiliateProgram.id == program_id))
        assert result.scalar_one_or_none() is None


async def test_cascade_delete_provider_removes_programs(async_session_maker):
    """Deleting a provider should cascade-delete its affiliate programs."""
    store = await _make_store(async_session_maker)
    provider = await _make_provider(async_session_maker, "Awin")

    async with async_session_maker() as db:
        program = AffiliateProgram(
            merchant_id=store.id,
            provider_id=provider.id,
            commission_value=5.0,
        )
        db.add(program)
        await db.commit()
        program_id = program.id

    # Delete the provider
    async with async_session_maker() as db:
        provider_obj = await db.get(AffiliateProvider, provider.id)
        await db.delete(provider_obj)
        await db.commit()

    # Program should be gone
    async with async_session_maker() as db:
        result = await db.execute(select(AffiliateProgram).where(AffiliateProgram.id == program_id))
        assert result.scalar_one_or_none() is None
