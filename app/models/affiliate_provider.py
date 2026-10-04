"""Affiliate provider registry.

A provider is an affiliate network (Rakuten, Awin, CJ, Admitad) that can
carry merchant programs. This model is the configuration backbone for the
affiliate routing engine — it does NOT store credentials, only a reference
to where credentials live (environment variable name).

The existing `Source` model handles coupon *ingestion* from these networks.
This model handles *monetization* — which networks can carry affiliate links,
how much we trust them, and how they're prioritized when routing clicks.
"""

import enum

from sqlalchemy import (
    JSON,
    Boolean,
    Enum,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin


class ProviderType(str, enum.Enum):
    """The kind of affiliate network."""

    cpa = "cpa"  # Cost-per-action (Awin, CJ, Rakuten, Admitad)
    cpc = "cpc"  # Cost-per-click
    hybrid = "hybrid"  # Both models supported


class ProviderStatus(str, enum.Enum):
    """Operational health of this provider's API and tracking."""

    active = "active"
    degraded = "degraded"  # API responding but with elevated error rate
    paused = "paused"  # Deliberately disabled by an operator
    unconfigured = "unconfigured"  # No credentials present yet


class AffiliateProvider(Base, UUIDPKMixin, TimestampMixin):
    """One affiliate network that can carry merchant programs.

    `priority` determines the default routing order when no performance data
    exists yet. Lower number = higher priority. This is a starting weight,
    not a hard ranking — the AffiliateRouter combines it with EPC, conversion
    rate, and tracking reliability when selecting a provider.

    `credentials_reference` is the name of an environment variable (e.g.
    "AWIN_API_KEY"), never the credential itself. This keeps secrets out of
    the database, the admin panel, and any export.
    """

    __tablename__ = "affiliate_providers"

    __table_args__ = (
        UniqueConstraint("slug", name="uq_affiliate_provider_slug"),
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(140), unique=True, index=True, nullable=False)
    provider_type: Mapped[ProviderType] = mapped_column(
        Enum(ProviderType, name="provider_type", native_enum=True),
        default=ProviderType.cpa,
        server_default=ProviderType.cpa.value,
        nullable=False,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )

    # Lower number = higher priority. Used as the default routing weight
    # before performance data is available.
    priority: Mapped[int] = mapped_column(
        Integer, default=100, server_default="100", nullable=False
    )

    # ISO 3166-1 alpha-2 markets this provider operates in. Empty means
    # "no restriction" — the router treats that as globally eligible.
    countries: Mapped[list[str]] = mapped_column(
        ARRAY(String(2)).with_variant(JSON(), "sqlite"),
        nullable=False,
        default=list,
        server_default="{}",
    )

    api_status: Mapped[ProviderStatus] = mapped_column(
        Enum(ProviderStatus, name="provider_status", native_enum=True),
        default=ProviderStatus.unconfigured,
        server_default=ProviderStatus.unconfigured.value,
        nullable=False,
    )
    tracking_status: Mapped[ProviderStatus] = mapped_column(
        Enum(ProviderStatus, name="provider_tracking_status", native_enum=True),
        default=ProviderStatus.unconfigured,
        server_default=ProviderStatus.unconfigured.value,
        nullable=False,
    )

    # Name of an environment variable holding the API key/secret.
    # Resolved at runtime by the ingestion/credentials module.
    credentials_reference: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Whether this provider supports sub-ID / click reference pass-through
    # for attribution. When true, the router can send our internal click_id
    # as a sub-ID parameter.
    subid_supported: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )

    # Whether this provider supports deep links (direct-to-product URLs).
    deeplink_supported: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )

    # Whether this provider supports conversion postbacks/webhooks.
    postback_supported: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )

    # Cookie duration in days (how long after a click a conversion is credited).
    # Null means "use the program-level default".
    default_cookie_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Provider-specific notes for operators (e.g. "Requires approval for each merchant").
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __str__(self) -> str:
        return self.name
