"""Phase 6 — Daily analytics stats model tests.

Tests for the daily aggregation models used by the admin dashboard.
"""

import uuid
from datetime import date

from app.models.daily_stats import (
    DailyCouponStats,
    DailyMerchantStats,
    DailyProviderStats,
    DailyTrafficStats,
)
from app.models.store import Store


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


async def test_create_daily_coupon_stats(async_session_maker):
    from app.models.category import Category
    from app.models.coupon import Coupon, DiscountType

    store = await _make_store(async_session_maker)
    async with async_session_maker() as db:
        cat = Category(name=f"Cat-{uuid.uuid4().hex[:6]}", slug=f"cat-{uuid.uuid4().hex[:6]}")
        db.add(cat)
        await db.commit()
        await db.refresh(cat)

        coupon = Coupon(
            title="Test Coupon",
            slug="test-coupon-" + uuid.uuid4().hex[:6],
            code="SAVE10",
            discount_type=DiscountType.percentage,
            discount_value=10,
            store_id=store.id,
            category_id=cat.id,
            destination_url="https://example.com",
        )
        db.add(coupon)
        await db.commit()
        await db.refresh(coupon)

        stats = DailyCouponStats(
            date=date(2026, 10, 4),
            coupon_id=coupon.id,
            impressions=100,
            clicks=50,
            conversions=5,
            revenue=25.0,
            confirmed_revenue=20.0,
            currency="USD",
            ctr=0.5,
            conversion_rate=0.1,
            epc=0.4,
        )
        db.add(stats)
        await db.commit()
        await db.refresh(stats)

        assert stats.id is not None
        assert stats.impressions == 100
        assert stats.clicks == 50
        assert float(stats.revenue) == 25.0


async def test_create_daily_merchant_stats(async_session_maker):
    store = await _make_store(async_session_maker)

    async with async_session_maker() as db:
        stats = DailyMerchantStats(
            date=date(2026, 10, 4),
            merchant_id=store.id,
            impressions=200,
            clicks=100,
            conversions=10,
            revenue=50.0,
            confirmed_revenue=40.0,
            currency="USD",
        )
        db.add(stats)
        await db.commit()
        await db.refresh(stats)

        assert stats.id is not None
        assert stats.impressions == 200


async def test_create_daily_provider_stats(async_session_maker):
    from app.models.affiliate_provider import AffiliateProvider, ProviderStatus

    async with async_session_maker() as db:
        provider = AffiliateProvider(
            name="Awin",
            slug="awin-" + uuid.uuid4().hex[:6],
            provider_type="cpa",
            api_status=ProviderStatus.active,
        )
        db.add(provider)
        await db.commit()
        await db.refresh(provider)

        stats = DailyProviderStats(
            date=date(2026, 10, 4),
            provider_id=provider.id,
            clicks=100,
            conversions=5,
            commission=25.0,
            confirmed_commission=20.0,
            currency="USD",
        )
        db.add(stats)
        await db.commit()
        await db.refresh(stats)

        assert stats.id is not None
        assert stats.clicks == 100


async def test_create_daily_traffic_stats(async_session_maker):
    async with async_session_maker() as db:
        stats = DailyTrafficStats(
            date=date(2026, 10, 4),
            source="seo",
            impressions=500,
            clicks=250,
            conversions=25,
            revenue=125.0,
            currency="USD",
        )
        db.add(stats)
        await db.commit()
        await db.refresh(stats)

        assert stats.id is not None
        assert stats.source == "seo"
        assert stats.impressions == 500
