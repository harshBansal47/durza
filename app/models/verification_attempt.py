"""Machine checks on a coupon, kept apart from human reports.

`CouponVerification` is a person's "worked for me". This table is our own
automated re-check — the periodic confirmation that a code is still live and
still produces the discount it claims.

Why they must not be merged: a single automated check that hits a bot wall or a
session expiry would otherwise drag down the success rate shown to shoppers,
making an honest community signal look unreliable. Keeping them separate means
the storefront can trust the human number, and the machine number is used only
for the job it is suited to — deciding when a code has gone stale enough to
unpublish.

`valid` is deliberately nullable. "We could not check this" is not the same
claim as "we checked and it failed", and conflating them would let a flaky
checker silently retire working codes.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin


class AttemptOutcome(str, enum.Enum):
    worked = "worked"
    failed = "failed"
    # Our checker could not tell — rate limited, bot wall, store changed layout.
    # Not the code's fault, and must never count against it.
    inconclusive = "inconclusive"


class VerificationAttempt(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "verification_attempts"
    __table_args__ = (
        # History is append-only, so the pair is not unique — but every read
        # asks "what did this checker last conclude about this coupon?", so the
        # index is ordered newest-first to let the answer stop at one row.
        Index(
            "ix_verification_attempt_coupon_checker",
            "coupon_id",
            "checker",
            "checked_at",
        ),
    )

    coupon_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("coupons.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Which automated checker ran ("playwright_checkout", "headless_fetch", ...).
    # Free text: checkers come and go, and a new one should not need a migration.
    checker: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    outcome: Mapped[AttemptOutcome] = mapped_column(
        Enum(AttemptOutcome, name="attempt_outcome", native_enum=True), nullable=False
    )
    # True only for `worked`. Mirrored so the common query
    # (`WHERE consecutive_failures >= n`) doesn't need to read the enum.
    valid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    # Why the checker concluded what it did — HTTP status, "checkout blocked",
    # "code rejected". Read by whoever debugs a code that vanished unexpectedly.
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __str__(self) -> str:
        return f"{self.checker}:{self.outcome.value}"
