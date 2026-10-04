import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.category import Category
    from app.models.coupon_source import CouponSource
    from app.models.store import Store


class DiscountType(str, enum.Enum):
    percentage = "percentage"
    fixed = "fixed"
    deal = "deal"


class CouponStatus(str, enum.Enum):
    """Lifecycle state maintained by the ingestion/verification pipeline.

    `is_active` is kept in sync (active <-> True) because the public API's
    existing `active_only` filters rely on it. The richer vocabulary exists so
    that *why* something is not showing can be answered, which a single
    active/failed/expired triple cannot:

    * `pending_validation` — ingested, not yet checked. New coupons land here
      rather than going live on a provider's word alone.
    * `validation_failed`  — a checker actually tried and it did not work.
    * `source_removed`     — the provider no longer lists it. NOT the same as
      expired: the code may still work, we simply stopped hearing about it.
    * `disabled`           — a human turned it off. Never re-enabled by a sweep,
      because a machine that cannot distinguish "a person decided this" from
      "the data looks wrong" will eventually undo a person's decision.
    """

    active = "active"
    pending_validation = "pending_validation"
    validation_failed = "validation_failed"
    failed = "failed"
    expired = "expired"
    source_removed = "source_removed"
    disabled = "disabled"


#: Statuses a shopper may be shown. Everything else is a staging state.
PUBLICLY_VISIBLE_STATUSES: tuple[CouponStatus, ...] = (CouponStatus.active,)

#: Statuses a scheduled sweep is allowed to change. `disabled` is absent on
#: purpose — see the `disabled` docstring above.
SWEEPABLE_STATUSES: tuple[CouponStatus, ...] = (
    CouponStatus.active,
    CouponStatus.pending_validation,
    CouponStatus.validation_failed,
    CouponStatus.source_removed,
)


class VerificationStatus(str, enum.Enum):
    """How much evidence we have that a coupon works.

    Separate from `CouponStatus` because the two answer different questions.
    Status says "should we show this"; verification says "how much should the
    shopper trust it". A coupon can be `active` and `unverified` (fresh from a
    feed nobody has checked) — which is a normal, honest state, not a failure.
    """

    unverified = "unverified"
    pending = "pending"
    verified = "verified"
    disputed = "disputed"
    failed = "failed"


class Coupon(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "coupons"

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(220), unique=True, index=True, nullable=False)
    # Nullable: "deals" without a code leave this empty.
    code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    discount_type: Mapped[DiscountType] = mapped_column(
        Enum(DiscountType, name="discount_type", native_enum=True), nullable=False
    )
    discount_value: Mapped[float | None] = mapped_column(
        Numeric(10, 2, asdecimal=False), nullable=True
    )
    store_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), index=True, nullable=False
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    # Resolved fallback affiliate URL. The *authority* for where to send a
    # visitor is `CouponSource.affiliate_url` (one row per provider) — see
    # `app/services/affiliate_router.py`. This column is kept because it is the
    # value every existing reader (the /go redirect's fallback path, the admin
    # detail view, hand-created coupons) already depends on, and denormalising
    # it there means no reader has to become N+1 to resolve one destination.
    destination_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    views_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    clicks_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Community verification ("worked for me" / "didn't work"), kept denormalized on the
    # coupon for fast reads; CouponVerification rows are the source of truth + audit trail.
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fail_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- Multi-source identity --------------------------------------------
    # The dedup key. For a coded coupon this is derived from
    # (merchant, normalized_code); for a code-less offer from a fingerprint of
    # merchant + normalized title + discount + offer type. See
    # `app.ingestion.dedupe`. Indexed because it is looked up on every ingested
    # record, but NOT unique: unique is enforced per (store, code) by the
    # merchant/coupon pair and collisions are resolved by the pipeline, which
    # has the context to decide, rather than by an IntegrityError.
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)

    # Code in canonical form, uppercased with separators stripped. The single
    # strongest identity signal across providers — "save20", "SAVE 20" and
    # "Save-20" are one code, and "save-20" is a different one.
    normalized_code: Mapped[str | None] = mapped_column(String(50), index=True, nullable=True)

    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ---- Trust and ranking -------------------------------------------------
    status: Mapped[CouponStatus] = mapped_column(
        Enum(CouponStatus, name="coupon_status", native_enum=True),
        default=CouponStatus.active,
        server_default=CouponStatus.active.value,
        nullable=False,
    )
    verification_status: Mapped[VerificationStatus] = mapped_column(
        Enum(VerificationStatus, name="verification_status", native_enum=True),
        default=VerificationStatus.unverified,
        server_default=VerificationStatus.unverified.value,
        nullable=False,
    )
    # 0..100, recomputed by `app.services.quality` whenever inputs change.
    # Drives the "Recommended" sort, so it is indexed.
    quality_score: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", index=True, nullable=False
    )

    # ISO 3166-1 alpha-2 market this offer was verified for. Null means global.
    country: Mapped[str | None] = mapped_column(String(2), index=True, nullable=True)

    # Provider-specific conditions worth keeping: minimum order value, whether
    # it is new-customers-only, eligible/excluded SKUs, currency of the fixed
    # discount. Providers disagree about these constantly and none of them are
    # reliably derivable, so the raw claims are kept rather than guessed at.
    restrictions: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    # Free-form categories a provider supplied, beyond our own taxonomy.
    source_categories: Mapped[list[str]] = mapped_column(
        ARRAY(String(80)).with_variant(JSON(), "sqlite"), nullable=False, default=list
    )

    # ---- Provenance --------------------------------------------------------
    # The source that *created* this canonical row, kept distinct from the
    # CouponSource links. Null for hand-created coupons.
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), index=True, nullable=True
    )
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Count of providers currently backing this coupon. Denormalized because the
    # public card renders "verified by 3 networks" on every listing row and a
    # correlated subquery per row is exactly the N+1 to avoid.
    source_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )

    # Read-side conveniences (the admin panel's dropdowns). The JSON API doesn't load them.
    store: Mapped["Store"] = relationship()
    category: Mapped["Category"] = relationship()
    sources: Mapped[list["CouponSource"]] = relationship(
        back_populates="coupon", lazy="selectin", order_by="CouponSource.created_at"
    )

    def __str__(self) -> str:
        return self.title
