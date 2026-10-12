"""The canonical merchant entity.

Named `Store` for historical reasons — it predates the multi-source work and is
referenced by 13 models, 14 routers, the admin panel and the entire frontend,
so renaming it would buy nothing but churn. It *is* the Merchant the
architecture calls for, and it carries every field that model is specified
with: a canonical `normalized_name`, a canonical `normalized_domain`, known
`aliases`, `categories` and an explicit `status`.

The reason those fields exist at all: four affiliate networks will hand us the
same retailer under four different spellings —

    "Nike"  /  "NIKE"  /  "Nike.com"  /  "www.nike.com"

Creating a Store row per spelling means the shopper sees four Nike pages with
one coupon between them, and every page competes with the other three in
search. `normalized_domain` is therefore the strongest identity signal we have
and is unique-indexed: one domain, one merchant, enforced by the database
rather than by application code that can be bypassed.
"""

import enum

from sqlalchemy import JSON, Boolean, Enum, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin


class StoreStatus(str, enum.Enum):
    """Lifecycle of a merchant record.

    `pending` is a real state, not a formality: a merchant first seen in one
    provider's feed has not been corroborated by anything yet. Showing a
    one-source merchant on a public page is how a coupon site ends up with
    brand-new storefronts that turn out to have been typos.
    """

    active = "active"
    pending = "pending"
    inactive = "inactive"
    merged = "merged"


class Store(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "stores"

    __table_args__ = (
        # The guarantee the whole dedupe story rests on: one row per domain.
        #
        # `normalized_domain` is nullable, and both Postgres and SQLite treat
        # NULLs as distinct in a UNIQUE constraint — which is exactly right. A
        # provider that gives us a name but no usable domain leaves it NULL and
        # is matched on `normalized_name` instead; every store that *does* have a
        # domain is guaranteed to own it. `index=True` on the column would be a
        # second, redundant index on a column a unique constraint already covers.
        UniqueConstraint("normalized_domain", name="uq_store_normalized_domain"),
    )

    name: Mapped[str] = mapped_column(String(150), nullable=False)
    slug: Mapped[str] = mapped_column(String(170), unique=True, index=True, nullable=False)
    logo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    website_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Optional per-store note, e.g. "We earn a commission from Amazon on this link."
    # Falls back to a sitewide default in the frontend when null — see the Trust page.
    commission_disclosure: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- Canonical identity -------------------------------------------------
    # Casefolded, punctuation-stripped name. Used to match providers that give a
    # name but no usable domain.
    normalized_name: Mapped[str] = mapped_column(String(160), index=True, nullable=False, default="")

    # Registrable-ish hostname, lowercased, without scheme, port, path, "www."
    # or leading "m." — the single most reliable cross-provider identity signal.
    # See `merchants.normalize_domain` for the canonical form.
    normalized_domain: Mapped[str | None] = mapped_column(String(190), nullable=True)

    # Alternate spellings this merchant is known by ("NIKE", "Nike Official
    # Store"). Lets a provider's odd naming map onto an existing row without a
    # fuzzy match, which is the safe direction to resolve ambiguity.
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(String(160)).with_variant(JSON(), "sqlite"), nullable=False, default=list
    )

    status: Mapped[StoreStatus] = mapped_column(
        Enum(StoreStatus, name="store_status", native_enum=True),
        nullable=False,
        default=StoreStatus.active,
        server_default=StoreStatus.active.value,
    )

    # Set when this row was folded into another (see StoreStatus.merged). Kept so
    # old coupon rows still resolve to a real merchant instead of a dead id.
    merged_into_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # ---- Affiliate wiring ---------------------------------------------------
    # `affiliate_network` picks how the click reference is attached to outbound
    # links; `link_template` overrides it for programmes that need a deeplink
    # wrapper (see app/services/affiliate.py). Never exposed by the public API.
    affiliate_network: Mapped[str] = mapped_column(
        String(20), default="none", server_default="none", nullable=False
    )
    link_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Informational: how long the network credits a click, for revenue analysis.
    cookie_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ---- Market -------------------------------------------------------------
    # Market data. Durza lists stores per country, so a store carries the
    # market it belongs to: ISO 3166-1 alpha-2 for routing/sitemaps, ISO 4217
    # for rendering prices. Both nullable — a global store (Amazon) may not
    # belong to one market.
    country_code: Mapped[str | None] = mapped_column(String(2), index=True, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)

    def __str__(self) -> str:
        return self.name
