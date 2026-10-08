"""One row per community "worked" / "didn't work" report on a coupon.

ip_hash (not the raw IP) both rate-limits repeat votes from the same visitor
and gives an audit trail, without storing anything raw and identifying.
"""

import uuid

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPKMixin


class CouponVerification(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "coupon_verifications"

    coupon_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("coupons.id", ondelete="CASCADE"), index=True, nullable=False
    )
    ip_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    worked: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Optional free text from the reporter ("code worked, min. spend excluded").
    # Shown on the coupon page as the reason behind a vote — useful when a code
    # works conditionally, which a bare worked/didn't-work flag cannot express.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
