"""Analytics session tracking.

An AnalyticsSession represents a single browsing session by a visitor.
It captures UTM parameters, referrer, device info, and landing page
for traffic source attribution.

Sessions are distinct from visitors: a visitor may have many sessions
over time. The visitor_id is stored in a first-party cookie (cz_vid),
while the session_id is stored in a session cookie (cz_sid).

No PII is stored. The visitor_id is a cryptographically random opaque
identifier with no connection to email, name, or any other personal data.
"""

import enum
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin


class DeviceType(str, enum.Enum):
    """Coarse device classification."""

    desktop = "desktop"
    mobile = "mobile"
    tablet = "tablet"
    other = "other"


class TrafficSource(str, enum.Enum):
    """How the visitor arrived at the site.

    Derived from UTM parameters and referrer. When no UTM parameters
    are present and the referrer is absent or internal, the source
    is "direct". When the referrer is a search engine, it's "seo".
    When the referrer is a social platform, it's "social".
    """

    seo = "seo"
    direct = "direct"
    social = "social"
    email = "email"
    referral = "referral"
    paid = "paid"
    other = "other"


class AnalyticsSession(Base, UUIDPKMixin, TimestampMixin):
    """One browsing session by a visitor.

    `visitor_id` is the opaque first-party cookie value (cz_vid). It is
    NOT derived from IP address, email, or any other PII. It is a
    cryptographically random UUID stored in a first-party cookie.

    `landing_page` and `landing_path` capture where the session started.
    UTM parameters are captured at session start and never overwritten,
    preserving the original acquisition data.

    `traffic_source` is computed from UTM + referrer at session creation.
    """

    __tablename__ = "analytics_sessions"

    __table_args__ = (
        # One session per visitor per day (a visitor may return multiple
        # times in a day, but each gets its own session row).
        # Actually, we want to track ALL sessions, not just one per day.
        # The unique constraint is on the session_id itself (the PK).
        # Index for looking up sessions by visitor.
        Index("ix_analytics_sessions_visitor", "visitor_id"),
        # Index for time-based queries (recent sessions, etc.)
        Index("ix_analytics_sessions_started", "started_at"),
        # Index for traffic source analysis
        Index("ix_analytics_sessions_source", "traffic_source"),
    )

    # The opaque visitor identifier (from cz_vid cookie).
    # NOT an IP hash, NOT email, NOT any PII.
    visitor_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # When the session started and was last seen.
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # ---- Landing page ---------------------------------------------------
    landing_page: Mapped[str | None] = mapped_column(String(500), nullable=True)
    landing_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # ---- Referrer -------------------------------------------------------
    referrer: Mapped[str | None] = mapped_column(Text, nullable=True)
    referrer_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ---- UTM parameters (captured at session start, never overwritten) --
    utm_source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    utm_medium: Mapped[str | None] = mapped_column(String(100), nullable=True)
    utm_campaign: Mapped[str | None] = mapped_column(String(200), nullable=True)
    utm_content: Mapped[str | None] = mapped_column(String(200), nullable=True)
    utm_term: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # ---- Device / browser -----------------------------------------------
    device_type: Mapped[DeviceType] = mapped_column(
        Enum(DeviceType, name="device_type", native_enum=True),
        default=DeviceType.other,
        server_default=DeviceType.other.value,
        nullable=False,
    )
    browser: Mapped[str | None] = mapped_column(String(100), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)

    # ---- Computed traffic source ----------------------------------------
    traffic_source: Mapped[TrafficSource] = mapped_column(
        Enum(TrafficSource, name="traffic_source", native_enum=True),
        default=TrafficSource.direct,
        server_default=TrafficSource.direct.value,
        nullable=False,
    )

    def __str__(self) -> str:
        return f"Session({self.visitor_id[:8]}…, {self.traffic_source.value})"
