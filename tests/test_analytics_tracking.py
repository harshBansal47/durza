"""Phase 2 — Identity & tracking tests.

Tests for analytics session tracking and coupon impression tracking.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.models.analytics_session import AnalyticsSession, DeviceType, TrafficSource
from app.models.coupon_impression import CouponImpression, ImpressionType
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


async def _make_coupon(async_session_maker, store_id, title="10% Off"):
    from app.models.category import Category
    from app.models.coupon import Coupon, DiscountType

    async with async_session_maker() as db:
        cat = Category(name=f"Cat-{uuid.uuid4().hex[:6]}", slug=f"cat-{uuid.uuid4().hex[:6]}")
        db.add(cat)
        await db.commit()
        await db.refresh(cat)

        coupon = Coupon(
            title=title,
            slug=title.lower().replace(" ", "-") + f"-{uuid.uuid4().hex[:6]}",
            code="SAVE10",
            discount_type=DiscountType.percentage,
            discount_value=10,
            store_id=store_id,
            category_id=cat.id,
            destination_url="https://example.com/deal",
        )
        db.add(coupon)
        await db.commit()
        await db.refresh(coupon)
        return coupon


# ------------------------------------------------------------------ sessions


async def test_create_session(async_session_maker):
    async with async_session_maker() as db:
        session = AnalyticsSession(
            visitor_id="a" * 64,
            started_at=datetime.now(UTC),
            last_seen_at=datetime.now(UTC),
            landing_page="/stores/nike",
            landing_path="/stores/nike",
            device_type=DeviceType.mobile,
            traffic_source=TrafficSource.seo,
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)

        assert session.id is not None
        assert session.visitor_id == "a" * 64
        assert session.landing_page == "/stores/nike"
        assert session.device_type == DeviceType.mobile
        assert session.traffic_source == TrafficSource.seo


async def test_session_with_utm(async_session_maker):
    async with async_session_maker() as db:
        session = AnalyticsSession(
            visitor_id="b" * 64,
            started_at=datetime.now(UTC),
            last_seen_at=datetime.now(UTC),
            utm_source="google",
            utm_medium="cpc",
            utm_campaign="nike-sale",
            utm_content="banner-1",
            utm_term="nike coupon",
            traffic_source=TrafficSource.paid,
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)

        assert session.utm_source == "google"
        assert session.utm_medium == "cpc"
        assert session.utm_campaign == "nike-sale"
        assert session.utm_content == "banner-1"
        assert session.utm_term == "nike coupon"
        assert session.traffic_source == TrafficSource.paid


async def test_session_with_referrer(async_session_maker):
    async with async_session_maker() as db:
        session = AnalyticsSession(
            visitor_id="c" * 64,
            started_at=datetime.now(UTC),
            last_seen_at=datetime.now(UTC),
            referrer="https://google.com/search?q=nike+coupon",
            referrer_domain="google.com",
            traffic_source=TrafficSource.seo,
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)

        assert session.referrer == "https://google.com/search?q=nike+coupon"
        assert session.referrer_domain == "google.com"
        assert session.traffic_source == TrafficSource.seo


async def test_session_enum_validation():
    assert DeviceType("desktop") == DeviceType.desktop
    assert DeviceType("mobile") == DeviceType.mobile
    assert DeviceType("tablet") == DeviceType.tablet
    assert DeviceType("other") == DeviceType.other
    assert TrafficSource("seo") == TrafficSource.seo
    assert TrafficSource("direct") == TrafficSource.direct
    assert TrafficSource("social") == TrafficSource.social
    assert TrafficSource("email") == TrafficSource.email
    assert TrafficSource("referral") == TrafficSource.referral
    assert TrafficSource("paid") == TrafficSource.paid
    assert TrafficSource("other") == TrafficSource.other


async def test_session_no_pii(async_session_maker):
    """Sessions must not store PII like email or phone."""
    async with async_session_maker() as db:
        session = AnalyticsSession(
            visitor_id="d" * 64,
            started_at=datetime.now(UTC),
            last_seen_at=datetime.now(UTC),
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)

        # Verify no PII fields exist
        assert not hasattr(session, "email")
        assert not hasattr(session, "phone")
        assert not hasattr(session, "name")
        assert not hasattr(session, "ip_address")


# ------------------------------------------------------------------ impressions


async def test_create_impression(async_session_maker):
    store = await _make_store(async_session_maker)
    coupon = await _make_coupon(async_session_maker, store.id)

    async with async_session_maker() as db:
        impression = CouponImpression(
            coupon_id=coupon.id,
            merchant_id=store.id,
            session_id="sess-123",
            visitor_id="e" * 64,
            page_type=ImpressionType.search_results,
            page_id="search-nike",
            position=1,
            ranking_version="v1",
            ranking_score=0.85,
            source="google",
            device_type="mobile",
            country="US",
            sponsored=False,
            created_at=datetime.now(UTC),
        )
        db.add(impression)
        await db.commit()
        await db.refresh(impression)

        assert impression.id is not None
        assert impression.coupon_id == coupon.id
        assert impression.merchant_id == store.id
        assert impression.position == 1
        assert impression.ranking_version == "v1"
        assert impression.ranking_score == 0.85
        assert impression.sponsored is False


async def test_sponsored_impression(async_session_maker):
    store = await _make_store(async_session_maker)
    coupon = await _make_coupon(async_session_maker, store.id)

    async with async_session_maker() as db:
        impression = CouponImpression(
            coupon_id=coupon.id,
            merchant_id=store.id,
            page_type=ImpressionType.sponsored_section,
            position=1,
            ranking_version="v1",
            sponsored=True,
            created_at=datetime.now(UTC),
        )
        db.add(impression)
        await db.commit()
        await db.refresh(impression)

        assert impression.sponsored is True
        assert impression.page_type == ImpressionType.sponsored_section


async def test_impression_enum_validation():
    assert ImpressionType("coupon_card") == ImpressionType.coupon_card
    assert ImpressionType("coupon_detail") == ImpressionType.coupon_detail
    assert ImpressionType("merchant_page") == ImpressionType.merchant_page
    assert ImpressionType("category_page") == ImpressionType.category_page
    assert ImpressionType("search_results") == ImpressionType.search_results
    assert ImpressionType("homepage") == ImpressionType.homepage
    assert ImpressionType("featured_section") == ImpressionType.featured_section
    assert ImpressionType("sponsored_section") == ImpressionType.sponsored_section


async def test_impression_cascade_delete_coupon(async_session_maker):
    """Deleting a coupon should cascade-delete its impressions."""
    store = await _make_store(async_session_maker)
    coupon = await _make_coupon(async_session_maker, store.id)

    async with async_session_maker() as db:
        impression = CouponImpression(
            coupon_id=coupon.id,
            merchant_id=store.id,
            page_type=ImpressionType.coupon_card,
            position=1,
            created_at=datetime.now(UTC),
        )
        db.add(impression)
        await db.commit()
        impression_id = impression.id

    # Delete the coupon
    async with async_session_maker() as db:
        from app.models.coupon import Coupon

        coupon_obj = await db.get(Coupon, coupon.id)
        await db.delete(coupon_obj)
        await db.commit()

    # Impression should be gone
    async with async_session_maker() as db:
        result = await db.execute(
            select(CouponImpression).where(CouponImpression.id == impression_id)
        )
        assert result.scalar_one_or_none() is None


async def test_impression_cascade_delete_store(async_session_maker):
    """Deleting a store should cascade-delete its impressions."""
    store = await _make_store(async_session_maker)
    coupon = await _make_coupon(async_session_maker, store.id)

    async with async_session_maker() as db:
        impression = CouponImpression(
            coupon_id=coupon.id,
            merchant_id=store.id,
            page_type=ImpressionType.merchant_page,
            position=1,
            created_at=datetime.now(UTC),
        )
        db.add(impression)
        await db.commit()
        impression_id = impression.id

    # Delete the store (which cascades to coupon, which cascades to impression)
    async with async_session_maker() as db:
        store_obj = await db.get(Store, store.id)
        await db.delete(store_obj)
        await db.commit()

    # Impression should be gone
    async with async_session_maker() as db:
        result = await db.execute(
            select(CouponImpression).where(CouponImpression.id == impression_id)
        )
        assert result.scalar_one_or_none() is None


async def test_multiple_impressions_same_coupon(async_session_maker):
    """A coupon can be impressioned multiple times across different pages."""
    store = await _make_store(async_session_maker)
    coupon = await _make_coupon(async_session_maker, store.id)

    async with async_session_maker() as db:
        impressions = [
            CouponImpression(
                coupon_id=coupon.id,
                merchant_id=store.id,
                page_type=ImpressionType.search_results,
                position=1,
                created_at=datetime.now(UTC),
            ),
            CouponImpression(
                coupon_id=coupon.id,
                merchant_id=store.id,
                page_type=ImpressionType.merchant_page,
                position=2,
                created_at=datetime.now(UTC),
            ),
            CouponImpression(
                coupon_id=coupon.id,
                merchant_id=store.id,
                page_type=ImpressionType.homepage,
                position=3,
                created_at=datetime.now(UTC),
            ),
        ]
        db.add_all(impressions)
        await db.commit()

        result = await db.execute(
            select(CouponImpression).where(CouponImpression.coupon_id == coupon.id)
        )
        found = result.scalars().all()
        assert len(found) == 3
        positions = {imp.position for imp in found}
        assert positions == {1, 2, 3}
