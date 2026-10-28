import csv
import io
from datetime import UTC, date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.core.database import get_db
from app.core.deps import require_role
from app.models.affiliate_provider import AffiliateProvider
from app.models.analytics_event import AnalyticsEvent
from app.models.analytics_session import AnalyticsSession
from app.models.daily_stats import (
    DailyCouponStats,
    DailyMerchantStats,
    DailyProviderStats,
    DailyTrafficStats,
)
from app.models.ingestion_run import IngestionRun
from app.models.job import JobRun, JobStatus
from app.models.store import Store
from app.models.user import Role, User
from app.models.verification_attempt import AttemptOutcome, VerificationAttempt
from app.schemas.admin_analytics import (
    AdminAnalyticsOverview,
    AffiliatePerformance,
    AnalyticsMeta,
    CouponPerformance,
    CurrencyRevenue,
    DataHealth,
    DeviceBreakdown,
    EngagementOverview,
    MerchantPerformance,
    ProviderPerformance,
    TrafficOverview,
    TrafficSourceStat,
)

router = APIRouter(prefix="/admin/analytics", tags=["admin-analytics"])

_MAX_DATE_RANGE_DAYS = 366
_INGESTION_STALE_AFTER = timedelta(days=2)
_ENGAGEMENT_EVENTS = (
    "search",
    "coupon_reveal",
    "copy_code",
    "save_coupon",
    "follow_store",
)


def _csv_cell(value: str | float) -> str | int | float:
    """Prevent spreadsheet applications from interpreting data as a formula."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{value}"
    return value


@router.get("/overview", response_model=AdminAnalyticsOverview)
async def get_analytics_overview(
    start_date: date = Query(..., description="First included UTC date (YYYY-MM-DD)"),
    end_date: date = Query(..., description="Last included UTC date (YYYY-MM-DD)"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(Role.admin, Role.editor, Role.analytics_viewer)),
) -> AdminAnalyticsOverview:
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date cannot be after end_date")
    if (end_date - start_date).days >= _MAX_DATE_RANGE_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"Date range cannot exceed {_MAX_DATE_RANGE_DAYS} days",
        )
    start_at = datetime.combine(start_date, time.min, tzinfo=UTC)
    end_at = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=UTC)
    date_filter = (DailyTrafficStats.date >= start_date, DailyTrafficStats.date <= end_date)

    traffic_totals = (
        (
            await db.execute(
                select(
                    func.coalesce(func.sum(DailyTrafficStats.impressions), 0).label("impressions"),
                    func.coalesce(func.sum(DailyTrafficStats.clicks), 0).label("clicks"),
                ).where(*date_filter)
            )
        )
        .mappings()
        .one()
    )

    session_totals = (
        (
            await db.execute(
                select(
                    func.count(func.distinct(AnalyticsSession.visitor_id)).label("visitors"),
                    func.count(AnalyticsSession.id).label("sessions"),
                ).where(
                    AnalyticsSession.started_at >= start_at, AnalyticsSession.started_at < end_at
                )
            )
        )
        .mappings()
        .one()
    )

    source_rows = (
        await db.execute(
            select(
                DailyTrafficStats.source,
                func.sum(DailyTrafficStats.clicks).label("clicks"),
            )
            .where(DailyTrafficStats.date >= start_date, DailyTrafficStats.date <= end_date)
            .group_by(DailyTrafficStats.source)
            .order_by(func.sum(DailyTrafficStats.clicks).desc(), DailyTrafficStats.source)
            .limit(5)
        )
    ).mappings()

    device_rows = (
        await db.execute(
            select(
                AnalyticsSession.device_type,
                func.count(AnalyticsSession.id).label("sessions"),
            )
            .where(AnalyticsSession.started_at >= start_at, AnalyticsSession.started_at < end_at)
            .group_by(AnalyticsSession.device_type)
            .order_by(AnalyticsSession.device_type)
        )
    ).mappings()

    event_rows = (
        await db.execute(
            select(AnalyticsEvent.name, func.count(AnalyticsEvent.id).label("event_count"))
            .where(
                AnalyticsEvent.created_at >= start_at,
                AnalyticsEvent.created_at < end_at,
                AnalyticsEvent.name.in_(_ENGAGEMENT_EVENTS),
            )
            .group_by(AnalyticsEvent.name)
        )
    ).mappings()
    event_counts = {row["name"]: row["event_count"] for row in event_rows}

    coupon_totals = (
        (
            await db.execute(
                select(
                    func.coalesce(func.sum(DailyCouponStats.impressions), 0).label("impressions"),
                    func.coalesce(func.sum(DailyCouponStats.clicks), 0).label("clicks"),
                ).where(DailyCouponStats.date >= start_date, DailyCouponStats.date <= end_date)
            )
        )
        .mappings()
        .one()
    )
    verification_counts = (
        (
            await db.execute(
                select(
                    func.count(VerificationAttempt.id)
                    .filter(VerificationAttempt.outcome == AttemptOutcome.worked)
                    .label("worked"),
                    func.count(VerificationAttempt.id)
                    .filter(VerificationAttempt.outcome == AttemptOutcome.failed)
                    .label("failed"),
                ).where(
                    VerificationAttempt.checked_at >= start_at,
                    VerificationAttempt.checked_at < end_at,
                )
            )
        )
        .mappings()
        .one()
    )
    merchant_rows = (
        await db.execute(
            select(
                DailyMerchantStats.merchant_id,
                Store.name.label("merchant_name"),
                func.sum(DailyMerchantStats.impressions).label("impressions"),
                func.sum(DailyMerchantStats.clicks).label("clicks"),
            )
            .join(Store, Store.id == DailyMerchantStats.merchant_id)
            .where(DailyMerchantStats.date >= start_date, DailyMerchantStats.date <= end_date)
            .group_by(DailyMerchantStats.merchant_id, Store.name)
            .order_by(func.sum(DailyMerchantStats.clicks).desc(), Store.name)
            .limit(5)
        )
    ).mappings()

    provider_totals = (
        (
            await db.execute(
                select(
                    func.coalesce(func.sum(DailyProviderStats.clicks), 0).label("clicks"),
                    func.coalesce(func.sum(DailyProviderStats.conversions), 0).label("conversions"),
                ).where(DailyProviderStats.date >= start_date, DailyProviderStats.date <= end_date)
            )
        )
        .mappings()
        .one()
    )
    currency_rows = (
        await db.execute(
            select(
                DailyProviderStats.currency,
                func.sum(DailyProviderStats.commission).label("estimated"),
                func.sum(DailyProviderStats.confirmed_commission).label("confirmed"),
            )
            .where(DailyProviderStats.date >= start_date, DailyProviderStats.date <= end_date)
            .group_by(DailyProviderStats.currency)
            .order_by(DailyProviderStats.currency)
        )
    ).mappings()
    provider_rows = (
        await db.execute(
            select(
                DailyProviderStats.provider_id,
                AffiliateProvider.name.label("provider_name"),
                DailyProviderStats.currency,
                func.sum(DailyProviderStats.clicks).label("clicks"),
                func.sum(DailyProviderStats.conversions).label("conversions"),
                func.sum(DailyProviderStats.commission).label("estimated"),
                func.sum(DailyProviderStats.confirmed_commission).label("confirmed"),
            )
            .join(AffiliateProvider, AffiliateProvider.id == DailyProviderStats.provider_id)
            .where(DailyProviderStats.date >= start_date, DailyProviderStats.date <= end_date)
            .group_by(
                DailyProviderStats.provider_id,
                AffiliateProvider.name,
                DailyProviderStats.currency,
            )
            .order_by(
                func.sum(DailyProviderStats.conversions).desc(),
                AffiliateProvider.name,
                DailyProviderStats.currency,
            )
            .limit(5)
        )
    ).mappings()

    health_start = datetime.now(UTC) - timedelta(days=7)
    failed_jobs = await db.scalar(
        select(func.count(JobRun.id)).where(
            JobRun.status == JobStatus.failed,
            JobRun.started_at >= health_start,
        )
    )
    latest_ingestion = await db.scalar(
        select(IngestionRun).order_by(IngestionRun.started_at.desc()).limit(1)
    )
    malformed_records = await db.scalar(
        select(func.coalesce(func.sum(IngestionRun.malformed), 0)).where(
            IngestionRun.started_at >= health_start
        )
    )
    now = datetime.now(UTC)
    latest_ingestion_at = latest_ingestion.started_at if latest_ingestion else None
    if latest_ingestion_at is not None and latest_ingestion_at.tzinfo is None:
        latest_ingestion_at = latest_ingestion_at.replace(tzinfo=UTC)
    stale_ingestion = (
        latest_ingestion_at is None or now - latest_ingestion_at > _INGESTION_STALE_AFTER
    )

    impressions = int(coupon_totals["impressions"] or 0)
    clicks = int(coupon_totals["clicks"] or 0)

    return AdminAnalyticsOverview(
        traffic=TrafficOverview(
            unique_visitors=int(session_totals["visitors"] or 0),
            total_sessions=int(session_totals["sessions"] or 0),
            total_impressions=int(traffic_totals["impressions"] or 0),
            total_clicks=int(traffic_totals["clicks"] or 0),
            top_sources=[
                TrafficSourceStat(source=row["source"], clicks=int(row["clicks"] or 0))
                for row in source_rows
            ],
            device_breakdown=[
                DeviceBreakdown(
                    device_type=(
                        row["device_type"].value
                        if hasattr(row["device_type"], "value")
                        else str(row["device_type"])
                    ),
                    sessions=int(row["sessions"]),
                )
                for row in device_rows
            ],
        ),
        engagement=EngagementOverview(
            searches=event_counts.get("search", 0),
            coupon_reveals=event_counts.get("coupon_reveal", 0),
            code_copies=event_counts.get("copy_code", 0),
            coupon_saves=event_counts.get("save_coupon", 0),
            store_follows=event_counts.get("follow_store", 0),
        ),
        coupons=CouponPerformance(
            impressions=impressions,
            clicks=clicks,
            ctr=round(clicks * 100 / impressions, 2) if impressions else 0.0,
            verification_success=int(verification_counts["worked"] or 0),
            verification_failed=int(verification_counts["failed"] or 0),
            top_merchants=[
                MerchantPerformance(
                    merchant_id=str(row["merchant_id"]),
                    merchant_name=row["merchant_name"],
                    impressions=int(row["impressions"] or 0),
                    clicks=int(row["clicks"] or 0),
                )
                for row in merchant_rows
            ],
        ),
        affiliate=AffiliatePerformance(
            outbound_clicks=int(provider_totals["clicks"] or 0),
            conversions=int(provider_totals["conversions"] or 0),
            revenue_by_currency=[
                CurrencyRevenue(
                    currency=row["currency"],
                    estimated_commission=float(row["estimated"] or 0),
                    confirmed_commission=float(row["confirmed"] or 0),
                )
                for row in currency_rows
            ],
            top_providers=[
                ProviderPerformance(
                    provider_id=str(row["provider_id"]),
                    provider_name=row["provider_name"],
                    currency=row["currency"],
                    clicks=int(row["clicks"] or 0),
                    conversions=int(row["conversions"] or 0),
                    estimated_commission=float(row["estimated"] or 0),
                    confirmed_commission=float(row["confirmed"] or 0),
                )
                for row in provider_rows
            ],
        ),
        data_health=DataHealth(
            failed_jobs_last_7d=int(failed_jobs or 0),
            stale_ingestion=stale_ingestion,
            latest_ingestion_status=(
                latest_ingestion.status.value if latest_ingestion else "unknown"
            ),
            latest_ingestion_time=latest_ingestion_at,
            malformed_records_last_7d=int(malformed_records or 0),
        ),
        meta=AnalyticsMeta(
            start_date=start_date,
            end_date=end_date,
            user_role=current_user.role.value,
        ),
    )


@router.get("/overview.csv", response_class=Response)
async def export_analytics_overview(
    start_date: date = Query(..., description="First included UTC date (YYYY-MM-DD)"),
    end_date: date = Query(..., description="Last included UTC date (YYYY-MM-DD)"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(Role.admin)),
) -> Response:
    overview = await get_analytics_overview(start_date, end_date, db, current_user)
    rows: list[list[str | int | float]] = [
        ["Section", "Metric", "Value", "Currency"],
        ["Traffic", "Unique visitors", overview.traffic.unique_visitors, ""],
        ["Traffic", "Sessions", overview.traffic.total_sessions, ""],
        ["Traffic", "Impressions", overview.traffic.total_impressions, ""],
        ["Traffic", "Clicks", overview.traffic.total_clicks, ""],
        ["Engagement", "Searches", overview.engagement.searches, ""],
        ["Engagement", "Coupon reveals", overview.engagement.coupon_reveals, ""],
        ["Engagement", "Code copies", overview.engagement.code_copies, ""],
        ["Engagement", "Coupon saves", overview.engagement.coupon_saves, ""],
        ["Engagement", "Store follows", overview.engagement.store_follows, ""],
        ["Coupons", "Impressions", overview.coupons.impressions, ""],
        ["Coupons", "Clicks", overview.coupons.clicks, ""],
        ["Coupons", "CTR (%)", overview.coupons.ctr, ""],
        ["Coupons", "Automated checks worked", overview.coupons.verification_success, ""],
        ["Coupons", "Automated checks failed", overview.coupons.verification_failed, ""],
        ["Affiliate", "Outbound clicks", overview.affiliate.outbound_clicks, ""],
        ["Affiliate", "Conversions", overview.affiliate.conversions, ""],
    ]
    for revenue in overview.affiliate.revenue_by_currency:
        rows.extend(
            [
                [
                    "Affiliate",
                    "Estimated commission",
                    revenue.estimated_commission,
                    revenue.currency,
                ],
                [
                    "Affiliate",
                    "Confirmed commission",
                    revenue.confirmed_commission,
                    revenue.currency,
                ],
            ]
        )

    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerows([[_csv_cell(value) for value in row] for row in rows])
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="analytics_{start_date.isoformat()}_'
                f'{end_date.isoformat()}.csv"'
            ),
            "Cache-Control": "private, no-store, max-age=0",
        },
    )
