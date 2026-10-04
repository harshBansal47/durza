"""One source's view of a canonical coupon.

This is the table that makes "one canonical coupon + multiple source records"
possible. When Rakuten, Awin, CJ and Admitad all advertise ``SAVE20`` at Nike,
the pipeline writes **one** `Coupon` row and **four** `CouponSource` rows.

Why the split exists at all, stated plainly: the providers disagree. One says
the discount expires on the 30th, another says the 31st. One has a deep link,
another has a voucher-tracking URL. If we collapsed everything onto a single
row, the only options are to pick a winner and silently discard the rest, or to
let the last writer overwrite the others — and both make the site's numbers
worse every time two networks happen to disagree.

So source-level facts stay here, and `Coupon` holds only the *resolved* public
representation that a shopper sees. Nothing is lost either way: `raw_data`
keeps the original provider payload for debugging, auditing and future
re-resolution.
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
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.coupon import Coupon
    from app.models.source import Source


class CouponSourceStatus(str, enum.Enum):
    """What *this* provider currently says about this coupon.

    Deliberately narrower than `CouponStatus`. A coupon is only `EXPIRED` once
    every source agrees (or a date says so); one source going quiet only
    downgrades that source's row. Collapsing the two vocabularies is how a
    single flaky API call ends up deleting a working coupon.
    """

    active = "active"
    inactive = "inactive"
    expired = "expired"
    failed = "failed"
    deleted = "deleted"


class CouponSource(Base, UUIDPKMixin, TimestampMixin):
    """A coupon as one specific provider describes it."""

    __tablename__ = "coupon_sources"

    __table_args__ = (
        # One row per (coupon, source). This is the constraint that makes the
        # upsert an upsert rather than an append, and it is what stops a
        # re-ingesting provider from fanning a coupon into N near-identical rows.
        UniqueConstraint("coupon_id", "source_id", name="uq_coupon_source"),
        # Look a source row up by the provider's own id — the primary identity
        # path during ingestion, before we know which canonical coupon it is.
        Index("ix_coupon_sources_source_external", "source_id", "external_id"),
        # "Every source backing this coupon" (coupon detail page, AffiliateRouter).
        Index("ix_coupon_sources_coupon_status", "coupon_id", "status"),
    )

    coupon_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("coupons.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )

    # The provider's own identifier for this offer. Their ID, not ours.
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # This provider's affiliate URL. Multiple rows for one coupon means multiple
    # candidate destinations — picking between them is AffiliateRouter's job.
    affiliate_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[CouponSourceStatus] = mapped_column(
        Enum(CouponSourceStatus, name="coupon_source_status", native_enum=True),
        nullable=False,
        default=CouponSourceStatus.active,
        server_default=CouponSourceStatus.active.value,
    )

    # What this provider claims the expiry is. Kept apart from `Coupon.expires_at`
    # so conflict resolution can compare the two claims without re-parsing JSON.
    source_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # The provider's own headline, kept for provenance. `Coupon.title` is the
    # resolved title and may have come from a different provider entirely.
    source_title: Mapped[str | None] = mapped_column(String(300), nullable=True)

    # The verbatim provider payload, for debugging, auditing and re-resolution.
    # JSON rather than JSONB so SQLite and Postgres behave identically in tests.
    raw_data: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    # Last time this provider reported the coupon at all. The basis of the
    # "not returned by one API call" distinction: absence is only meaningful
    # once the source has actually been reached.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Last time this source's data was actively confirmed rather than merely
    # re-reported.
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Source-side timestamps, which routinely disagree with ours.
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    coupon: Mapped["Coupon"] = relationship(back_populates="sources")
    source: Mapped["Source"] = relationship(back_populates="coupon_sources")

    def __str__(self) -> str:
        return f"{self.source.name}:{self.external_id}"
