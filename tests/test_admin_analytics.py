import uuid
from datetime import UTC, date, datetime, timedelta

from app.models.affiliate_provider import AffiliateProvider, ProviderStatus
from app.models.analytics_event import AnalyticsEvent
from app.models.analytics_session import AnalyticsSession, DeviceType, TrafficSource
from app.models.category import Category
from app.models.coupon import Coupon, DiscountType
from app.models.daily_stats import (
    DailyCouponStats,
    DailyMerchantStats,
    DailyProviderStats,
    DailyTrafficStats,
)
from app.models.ingestion_run import IngestionRun, RunStatus, SyncMode
from app.models.store import Store
from app.models.user import Role
from app.models.verification_attempt import AttemptOutcome, VerificationAttempt

URL = "/api/v1/admin/analytics/overview"


async def test_overview_requires_staff_role(client, user_headers):
    response = await client.get(
        URL,
        params={"start_date": "2026-10-01", "end_date": "2026-10-28"},
        headers=user_headers,
    )
    assert response.status_code == 403


async def test_overview_aggregates_date_bounded_analytics(
    client, admin_headers, async_session_maker
):
    start = date(2026, 10, 27)
    end = date(2026, 10, 28)
    async with async_session_maker() as db:
        store = Store(
            name="Analytics Merchant",
            slug=f"analytics-merchant-{uuid.uuid4().hex[:8]}",
            normalized_name="analytics merchant",
            normalized_domain="analytics-merchant.example",
        )
        category = Category(
            name=f"Analytics Category {uuid.uuid4().hex[:8]}",
            slug=f"analytics-category-{uuid.uuid4().hex[:8]}",
        )
        db.add_all([store, category])
        await db.flush()
        coupon = Coupon(
            title="Analytics Coupon",
            slug=f"analytics-coupon-{uuid.uuid4().hex[:8]}",
            discount_type=DiscountType.percentage,
            discount_value=10,
            store_id=store.id,
            category_id=category.id,
            destination_url="https://example.com/offer",
        )
        db.add(coupon)
        await db.flush()
        db.add_all(
            [
                AnalyticsSession(
                    visitor_id="visitor-1",
                    started_at=datetime(2026, 10, 27, 12, tzinfo=UTC),
                    last_seen_at=datetime(2026, 10, 27, 12, tzinfo=UTC),
                    device_type=DeviceType.mobile,
                    traffic_source=TrafficSource.direct,
                ),
                AnalyticsSession(
                    visitor_id="visitor-1",
                    started_at=datetime(2026, 10, 28, 12, tzinfo=UTC),
                    last_seen_at=datetime(2026, 10, 28, 12, tzinfo=UTC),
                    device_type=DeviceType.mobile,
                    traffic_source=TrafficSource.direct,
                ),
                AnalyticsSession(
                    visitor_id="outside-range",
                    started_at=datetime(2026, 10, 29, tzinfo=UTC),
                    last_seen_at=datetime(2026, 10, 29, tzinfo=UTC),
                    device_type=DeviceType.desktop,
                    traffic_source=TrafficSource.seo,
                ),
                AnalyticsEvent(
                    name="search",
                    created_at=datetime(2026, 10, 28, 23, 59, tzinfo=UTC),
                ),
                AnalyticsEvent(
                    name="coupon_reveal",
                    created_at=datetime(2026, 10, 29, tzinfo=UTC),
                ),
                DailyTrafficStats(date=start, source="seo", impressions=80, clicks=12),
                DailyTrafficStats(date=end, source="direct", impressions=20, clicks=8),
                DailyTrafficStats(date=date(2026, 10, 29), source="outside", clicks=100),
                DailyCouponStats(
                    date=start,
                    coupon_id=coupon.id,
                    impressions=40,
                    clicks=10,
                    currency="USD",
                ),
                DailyMerchantStats(
                    date=start,
                    merchant_id=store.id,
                    impressions=40,
                    clicks=10,
                    currency="USD",
                ),
                VerificationAttempt(
                    coupon_id=coupon.id,
                    checker="analytics-test",
                    outcome=AttemptOutcome.worked,
                    valid=True,
                    checked_at=datetime(2026, 10, 27, 12, tzinfo=UTC),
                ),
                VerificationAttempt(
                    coupon_id=coupon.id,
                    checker="analytics-test",
                    outcome=AttemptOutcome.failed,
                    valid=False,
                    checked_at=datetime(2026, 10, 28, 12, tzinfo=UTC),
                ),
            ]
        )
        provider = AffiliateProvider(
            name="Example Network",
            slug=f"example-{uuid.uuid4().hex[:8]}",
            provider_type="cpa",
            api_status=ProviderStatus.active,
        )
        db.add(provider)
        await db.flush()
        db.add_all(
            [
                DailyProviderStats(
                    date=start,
                    provider_id=provider.id,
                    clicks=2,
                    conversions=1,
                    commission=5.0,
                    confirmed_commission=3.0,
                    currency="USD",
                ),
                DailyProviderStats(
                    date=end,
                    provider_id=provider.id,
                    clicks=3,
                    conversions=2,
                    commission=4.0,
                    confirmed_commission=2.0,
                    currency="EUR",
                ),
            ]
        )
        latest_run = IngestionRun(
            source_id=await _source_id(db),
            status=RunStatus.partial,
            mode=SyncMode.incremental,
            started_at=datetime.now(UTC) - timedelta(hours=1),
            malformed=4,
        )
        db.add(latest_run)
        await db.commit()

    response = await client.get(
        URL,
        params={"start_date": start.isoformat(), "end_date": end.isoformat()},
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["traffic"]["unique_visitors"] == 1
    assert data["traffic"]["total_sessions"] == 2
    assert data["traffic"]["total_impressions"] == 100
    assert data["traffic"]["total_clicks"] == 20
    assert data["traffic"]["top_sources"] == [
        {"source": "seo", "clicks": 12},
        {"source": "direct", "clicks": 8},
    ]
    assert data["traffic"]["device_breakdown"] == [{"device_type": "mobile", "sessions": 2}]
    assert data["engagement"]["searches"] == 1
    assert data["engagement"]["coupon_reveals"] == 0
    assert data["coupons"]["impressions"] == 40
    assert data["coupons"]["clicks"] == 10
    assert data["coupons"]["ctr"] == 25.0
    assert data["coupons"]["verification_success"] == 1
    assert data["coupons"]["verification_failed"] == 1
    assert data["coupons"]["top_merchants"] == [
        {
            "merchant_id": str(store.id),
            "merchant_name": "Analytics Merchant",
            "impressions": 40,
            "clicks": 10,
        }
    ]
    assert data["affiliate"]["outbound_clicks"] == 5
    assert data["affiliate"]["conversions"] == 3
    assert data["affiliate"]["revenue_by_currency"] == [
        {"currency": "EUR", "estimated_commission": 4.0, "confirmed_commission": 2.0},
        {"currency": "USD", "estimated_commission": 5.0, "confirmed_commission": 3.0},
    ]
    assert data["data_health"]["latest_ingestion_status"] == "partial"
    assert data["data_health"]["malformed_records_last_7d"] == 4
    assert data["data_health"]["stale_ingestion"] is False
    assert data["meta"]["user_role"] == "admin"


async def _source_id(db):
    from app.models.source import Source, SourceKind

    source = Source(
        name=f"Analytics source {uuid.uuid4().hex[:8]}",
        slug=f"analytics-source-{uuid.uuid4().hex[:8]}",
        kind=SourceKind.feed,
    )
    db.add(source)
    await db.flush()
    return source.id


async def test_overview_rejects_invalid_and_oversized_date_ranges(client, admin_headers):
    reverse = await client.get(
        URL,
        params={"start_date": "2026-10-29", "end_date": "2026-10-28"},
        headers=admin_headers,
    )
    oversized = await client.get(
        URL,
        params={"start_date": "2025-01-01", "end_date": "2026-10-28"},
        headers=admin_headers,
    )
    assert reverse.status_code == 400
    assert oversized.status_code == 400


async def test_analytics_viewer_can_access_overview(client, make_headers):
    headers = await make_headers(role=Role.analytics_viewer)
    response = await client.get(
        URL,
        params={"start_date": "2026-10-28", "end_date": "2026-10-28"},
        headers=headers,
    )
    assert response.status_code == 200


async def test_csv_export_is_admin_only_and_downloadable(client, admin_headers, editor_headers):
    params = {"start_date": "2026-10-28", "end_date": "2026-10-28"}
    denied = await client.get(
        f"{URL}.csv",
        params=params,
        headers=editor_headers,
    )
    assert denied.status_code == 403

    response = await client.get(f"{URL}.csv", params=params, headers=admin_headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment;" in response.headers["content-disposition"]
    assert "Unique visitors" in response.text
