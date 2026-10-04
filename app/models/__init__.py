"""Import every model so SQLAlchemy's registry is complete wherever `app.models` is used.

`alembic/env.py` deliberately does NOT import this package — it lists model
modules explicitly. That is not redundancy: an implicit package import here
would make autogenerate silently sensitive to import order, and a module
forgotten in `env.py` is a table whose drift nobody will ever see. The check
that catches exactly that omission is `tests/test_models_registered.py`.
"""

from app.models.ad import Ad
from app.models.affiliate_destination import AffiliateDestination, DestinationStatus
from app.models.affiliate_program import (
    AffiliateProgram,
    CommissionType,
    ProgramStatus,
)
from app.models.affiliate_provider import (
    AffiliateProvider,
    ProviderStatus,
    ProviderType,
)
from app.models.affiliate_revenue import AffiliateRevenue, RevenueStatus
from app.models.analytics_session import (
    AnalyticsSession,
    DeviceType,
    TrafficSource,
)
from app.models.attribution import Attribution, AttributionModel
from app.models.category import Category
from app.models.click import ClickEvent
from app.models.conversion import Conversion, ConversionStatus
from app.models.coupon import (
    PUBLICLY_VISIBLE_STATUSES,
    SWEEPABLE_STATUSES,
    Coupon,
    CouponStatus,
    DiscountType,
    VerificationStatus,
)
from app.models.coupon_impression import CouponImpression, ImpressionType
from app.models.coupon_source import CouponSource, CouponSourceStatus
from app.models.coupon_verification import CouponVerification
from app.models.daily_stats import (
    DailyCouponStats,
    DailyMerchantStats,
    DailyProviderStats,
    DailyTrafficStats,
)
from app.models.ingestion_run import IngestionRun, RunStatus, SyncMode
from app.models.job import JobRun, JobStatus
from app.models.page import Page
from app.models.product import PricePoint, Product
from app.models.revenue_event import RevenueEvent, RevenueSourceType
from app.models.source import Source, SourceKind, SourceStatus
from app.models.sponsored import (
    CampaignModel,
    CampaignStatus,
    PlacementType,
    SponsoredCampaign,
    SponsoredOffer,
    SponsoredPlacement,
)
from app.models.store import Store, StoreStatus
from app.models.tracking import (
    AlertEvent,
    NotificationPreference,
    SavedCoupon,
    SavedStore,
    TrackAlertState,
    TrackedProduct,
)
from app.models.user import User
from app.models.verification_attempt import AttemptOutcome, VerificationAttempt

__all__ = [
    "PUBLICLY_VISIBLE_STATUSES",
    "SWEEPABLE_STATUSES",
    "Ad",
    "AffiliateDestination",
    "AffiliateProgram",
    "AffiliateProvider",
    "AffiliateRevenue",
    "AlertEvent",
    "AnalyticsSession",
    "AttemptOutcome",
    "Attribution",
    "AttributionModel",
    "CampaignModel",
    "CampaignStatus",
    "Category",
    "ClickEvent",
    "CommissionType",
    "Conversion",
    "ConversionStatus",
    "Coupon",
    "CouponImpression",
    "CouponSource",
    "CouponSourceStatus",
    "CouponStatus",
    "CouponVerification",
    "DailyCouponStats",
    "DailyMerchantStats",
    "DailyProviderStats",
    "DailyTrafficStats",
    "DestinationStatus",
    "DeviceType",
    "DiscountType",
    "ImpressionType",
    "IngestionRun",
    "JobRun",
    "JobStatus",
    "NotificationPreference",
    "Page",
    "PlacementType",
    "PricePoint",
    "Product",
    "ProgramStatus",
    "ProviderStatus",
    "ProviderType",
    "RevenueEvent",
    "RevenueSourceType",
    "RevenueStatus",
    "RunStatus",
    "SavedCoupon",
    "SavedStore",
    "Source",
    "SourceKind",
    "SourceStatus",
    "SponsoredCampaign",
    "SponsoredOffer",
    "SponsoredPlacement",
    "Store",
    "StoreStatus",
    "SyncMode",
    "TrackAlertState",
    "TrackedProduct",
    "TrafficSource",
    "User",
    "VerificationAttempt",
    "VerificationStatus",
]
