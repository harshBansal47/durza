"""Sponsored placement architecture.

Separate from organic coupon records. A sponsored campaign groups
one or more placements and offers. This allows a merchant to sponsor
specific coupons, categories, or pages without duplicating coupon
records.

The system clearly distinguishes:
- Organic impressions/clicks/conversions
- Sponsored impressions/clicks/conversions

API responses include a `type` field: "organic" or "sponsored".
Frontend must visibly disclose sponsored content.
"""

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.coupon import Coupon
    from app.models.store import Store


class CampaignStatus(str, enum.Enum):
    draft = "draft"
    active = "active"
    paused = "paused"
    ended = "ended"


class CampaignModel(str, enum.Enum):
    """How the campaign is priced/billed."""

    cpm = "cpm"  # Cost per mille impressions
    cpc = "cpc"  # Cost per click
    cpa = "cpa"  # Cost per action (conversion)
    fixed_placement = "fixed_placement"  # Fixed price per placement
    fixed_monthly = "fixed_monthly"  # Fixed monthly fee


class PlacementType(str, enum.Enum):
    homepage = "homepage"
    merchant_page = "merchant_page"
    category_page = "category_page"
    search = "search"
    coupon_list = "coupon_list"
    featured_section = "featured_section"


class SponsoredCampaign(Base, UUIDPKMixin, TimestampMixin):
    """A merchant's paid sponsorship campaign.

    `budget` and `currency` are the campaign's total spend. `start_at`
    and `end_at` define the active window. Budget is tracked but not
    automatically decremented — that's handled by a billing system.
    """

    __tablename__ = "sponsored_campaigns"

    merchant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[CampaignStatus] = mapped_column(
        Enum(CampaignStatus, name="campaign_status", native_enum=True),
        default=CampaignStatus.draft,
        server_default=CampaignStatus.draft.value,
        nullable=False,
    )
    start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    end_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    budget: Mapped[float | None] = mapped_column(
        Numeric(12, 2, asdecimal=False), nullable=True
    )
    currency: Mapped[str] = mapped_column(
        String(3), default="USD", server_default="USD", nullable=False
    )
    priority: Mapped[int] = mapped_column(
        Integer, default=100, server_default="100", nullable=False
    )
    campaign_model: Mapped[CampaignModel] = mapped_column(
        Enum(CampaignModel, name="campaign_model", native_enum=True),
        default=CampaignModel.fixed_placement,
        server_default=CampaignModel.fixed_placement.value,
        nullable=False,
    )
    targeting_rules: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    merchant: Mapped["Store"] = relationship()

    def __str__(self) -> str:
        return self.name


class SponsoredPlacement(Base, UUIDPKMixin, TimestampMixin):
    """A specific placement within a campaign.

    Defines where on the site the sponsorship appears (homepage,
    merchant page, search, etc.), what position rule applies, and
    frequency capping rules.
    """

    __tablename__ = "sponsored_placements"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sponsored_campaigns.id", ondelete="CASCADE"), nullable=False
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=False
    )
    placement_type: Mapped[PlacementType] = mapped_column(
        Enum(PlacementType, name="placement_type", native_enum=True),
        nullable=False,
    )
    page_type: Mapped[str] = mapped_column(String(50), nullable=False)
    position_rule: Mapped[str] = mapped_column(
        String(50), default="fixed", server_default="fixed", nullable=False
    )
    targeting: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    priority: Mapped[int] = mapped_column(
        Integer, default=100, server_default="100", nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), default="active", server_default="active", nullable=False
    )
    start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    end_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Frequency capping: max impressions per visitor per day
    max_impressions_per_visitor_per_day: Mapped[int] = mapped_column(
        Integer, default=3, server_default="3", nullable=False
    )

    campaign: Mapped["SponsoredCampaign"] = relationship()
    merchant: Mapped["Store"] = relationship()

    def __str__(self) -> str:
        return f"Placement({self.placement_type.value}, {self.page_type})"


class SponsoredOffer(Base, UUIDPKMixin, TimestampMixin):
    """A specific coupon/offer being sponsored.

    Links a campaign to a specific coupon (or merchant-level
    sponsorship if coupon_id is null). The coupon itself is NOT
    duplicated — this is just a pointer.
    """

    __tablename__ = "sponsored_offers"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sponsored_campaigns.id", ondelete="CASCADE"), nullable=False
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=False
    )
    coupon_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("coupons.id", ondelete="SET NULL"), nullable=True
    )

    campaign: Mapped["SponsoredCampaign"] = relationship()
    merchant: Mapped["Store"] = relationship()
    coupon: Mapped["Coupon | None"] = relationship()

    def __str__(self) -> str:
        return f"SponsoredOffer({self.campaign_id}, {self.coupon_id})"
