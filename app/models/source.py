"""Which adapter turns a source's config into offers, and how much we trust it.

`SourceKind` says *how* to fetch. The rest of this model says *whether to*, *how
often*, and — the part that actually makes multi-source merging work — *whom to
believe when two providers disagree*.

That last question is `priority` and `confidence`. They are data, not code, so
re-ordering who wins a conflict is a config change an operator can make and
audit, rather than a redeploy.

Credentials never live here. `credential_ref` is the *name* of an environment
variable; the value is read at fetch time from the process environment. That
keeps them out of the database, out of `/admin`'s detail view (which renders
`config` read-only, and is visible to every editor), and out of any future
export. See `app.ingestion.credentials`.
"""

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.coupon_source import CouponSource


class SourceKind(str, enum.Enum):
    """Which adapter turns this source's config into RawOffers."""

    csv = "csv"
    static = "static"  # inline offer list in config — used for tests and seed data
    feed = "feed"  # JSON over HTTP — the path to real automated sources
    awin = "awin"  # Awin (Affiliate Window) API — global affiliate network
    rakuten = "rakuten"  # Rakuten Advertising API
    cj = "cj"  # CJ Affiliate (Commission Junction) API
    admitad = "admitad"  # Admitad API
    impact = "impact"  # Impact (formerly Impact Radius) API


class SourceStatus(str, enum.Enum):
    """Independent health of one provider. Never a global on/off switch.

    A provider that keeps failing becomes `degraded` — which slows its schedule
    and hides it from public source counts, but leaves every coupon it already
    contributed intact and serving. Collapsing "this feed is broken" into
    "take the whole site down" is how one bad API key takes out a catalogue.
    """

    healthy = "healthy"
    degraded = "degraded"
    failing = "failing"
    paused = "paused"  # deliberately disabled by an operator
    unconfigured = "unconfigured"  # no credentials present yet


class Source(Base, UUIDPKMixin, TimestampMixin):
    """A place coupons come from. `config` is adapter-specific JSON.

    csv:    {"path": "/data/offers.csv", "default_category_slug": "general"}
    static: {"offers": [{...RawOffer fields...}]}
    feed:   {"url": "https://example.com/offers.json"}  # a JSON list, or {"offers": [...]}
    awin:   {"publisher_id": "...", "advertiser_ids": [...], "program_status": "active",
             "deal_types": ["coupon", "deal", "sale"], "currency": "USD",
             "regions": ["US", "GB", "DE", "FR"]}

    Credentials for every network-backed kind are NOT in here. Set
    `credential_ref` to the name of an env var and the adapter reads it.
    """

    __tablename__ = "sources"

    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(140), unique=True, index=True, nullable=False)
    kind: Mapped[SourceKind] = mapped_column(
        Enum(SourceKind, name="source_kind", native_enum=True), nullable=False
    )
    config: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=dict
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # When true, the pipeline issues a live HTTP probe of each offer's
    # destination_url and marks unreachable offers `failed`.
    probe_destinations: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- Precedence --------------------------------------------------------
    # Lower number wins a metadata conflict (0 = most trusted). Kept as an
    # explicit integer rather than derived from `kind` so operators can express
    # "we trust this advertiser's CJ feed more than their Rakuten feed".
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default="100", nullable=False)

    # 0..1. How much this provider's *own metadata* is trusted when resolving
    # conflicts, after priority. Separated from priority because a provider can
    # be highly trusted for "does this code exist" (existence is checkable) and
    # poorly trusted for "how much is it worth" (discount values are often
    # approximations). Configurable, and clamped on write.
    confidence: Mapped[float] = mapped_column(
        Float, default=0.5, server_default="0.5", nullable=False
    )

    # ---- Scope -------------------------------------------------------------
    # ISO 3166-1 alpha-2 markets this source is authoritative for. Empty means
    # "no opinion" — the affiliate router treats that as globally eligible,
    # because a source with no market restriction is more likely to be a global
    # feed than a mislabelled one.
    countries: Mapped[list[str]] = mapped_column(
        ARRAY(String(2)).with_variant(JSON(), "sqlite"), nullable=False, default=list
    )

    # ---- Credential reference (never the credential itself) ----------------
    # Name of an environment variable, e.g. "AWIN_API_KEY". Resolved at fetch
    # time by app.ingestion.credentials. Stored as a name precisely so that
    # rendering this row in the admin panel cannot leak a secret.
    credential_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # ---- Runtime limits ----------------------------------------------------
    rate_limit_per_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Client-side pacing even for networks without a published limit. A
    # provider that rate-limits by 429 rather than by documentation still needs
    # us to back off before it starts refusing us.
    requests_per_second: Mapped[float | None] = mapped_column(Float, nullable=True)
    sync_interval_minutes: Mapped[int] = mapped_column(
        Integer, default=360, server_default="360", nullable=False
    )
    # Per-request ceiling. A hung provider must not hold a worker open forever.
    timeout_seconds: Mapped[float] = mapped_column(
        Float, default=60.0, server_default="60.0", nullable=False
    )

    # ---- Health ------------------------------------------------------------
    # Kept denormalized on the row so the admin dashboard and /health can answer
    # "is CJ healthy?" without aggregating the whole ingestion_runs history on
    # every page load.
    api_status: Mapped[SourceStatus] = mapped_column(
        Enum(SourceStatus, name="source_status", native_enum=True),
        nullable=False,
        default=SourceStatus.healthy,
        server_default=SourceStatus.healthy.value,
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __str__(self) -> str:
        return self.name

    @property
    def precedence(self) -> tuple[int, float, str]:
        """The one ordering key for "who do we trust more", ascending.

        Exists so that trust has a single implementation. Three separate places
        need this ordering — conflict resolution during ingestion, resolving an
        incoming offer against what is already stored, and choosing where a
        click goes — and three copies of a sort key is three chances for them to
        disagree. A coupon whose metadata came from source A and whose deep link
        comes from source B would then be explained by two different orderings,
        which is worse than either ordering being wrong.

        The `slug` term exists purely to make the key *total*. Every source
        defaults to `priority=100, confidence=0.5`, so the first two components
        tie constantly; without a final tiebreak the winner would depend on the
        order the database happened to return rows in, and the same inputs would
        resolve differently on two runs — which makes every conflict
        unreproducible.
        """
        return (self.priority, -self.confidence, self.slug)

    # One-to-many: a source can have many coupon source records
    coupon_sources: Mapped[list["CouponSource"]] = relationship(
        back_populates="source", lazy="selectin"
    )
