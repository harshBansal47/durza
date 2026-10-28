from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class AnalyticsSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TrafficSourceStat(AnalyticsSchema):
    source: str
    clicks: int


class DeviceBreakdown(AnalyticsSchema):
    device_type: str
    sessions: int


class MerchantPerformance(AnalyticsSchema):
    merchant_id: str
    merchant_name: str
    impressions: int
    clicks: int


class ProviderPerformance(AnalyticsSchema):
    provider_id: str
    provider_name: str
    currency: str
    clicks: int
    conversions: int
    estimated_commission: float
    confirmed_commission: float


class CurrencyRevenue(AnalyticsSchema):
    currency: str
    estimated_commission: float
    confirmed_commission: float


class TrafficOverview(AnalyticsSchema):
    unique_visitors: int = 0
    total_sessions: int = 0
    total_impressions: int = 0
    total_clicks: int = 0
    top_sources: list[TrafficSourceStat] = Field(default_factory=list)
    device_breakdown: list[DeviceBreakdown] = Field(default_factory=list)


class EngagementOverview(AnalyticsSchema):
    searches: int = 0
    coupon_reveals: int = 0
    code_copies: int = 0
    coupon_saves: int = 0
    store_follows: int = 0


class CouponPerformance(AnalyticsSchema):
    impressions: int = 0
    clicks: int = 0
    ctr: float = 0.0
    verification_success: int = 0
    verification_failed: int = 0
    top_merchants: list[MerchantPerformance] = Field(default_factory=list)


class AffiliatePerformance(AnalyticsSchema):
    outbound_clicks: int = 0
    conversions: int = 0
    revenue_by_currency: list[CurrencyRevenue] = Field(default_factory=list)
    top_providers: list[ProviderPerformance] = Field(default_factory=list)


class DataHealth(AnalyticsSchema):
    failed_jobs_last_7d: int = 0
    stale_ingestion: bool = False
    latest_ingestion_status: str = "unknown"
    latest_ingestion_time: datetime | None = None
    malformed_records_last_7d: int = 0


class AnalyticsMeta(AnalyticsSchema):
    start_date: date
    end_date: date
    timezone: str = "UTC"
    user_role: str


class AdminAnalyticsOverview(AnalyticsSchema):
    traffic: TrafficOverview
    engagement: EngagementOverview
    coupons: CouponPerformance
    affiliate: AffiliatePerformance
    data_health: DataHealth
    meta: AnalyticsMeta
