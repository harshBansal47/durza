"""The view anonymous visitors (and AI agents) get: no destination_url.

The real URL is only ever resolved server-side by the /go/{id} redirect, so a
visitor — human or agent — can browse and decide, but can't scrape the raw
affiliate link straight out of the API response.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.models.coupon import DiscountType, VerificationStatus


def compute_success_rate(success_count: int, fail_count: int) -> float | None:
    total = success_count + fail_count
    return round(success_count / total, 3) if total else None


class CouponPublicRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    slug: str
    code: str | None
    description: str | None
    discount_type: DiscountType
    discount_value: float | None
    store_id: uuid.UUID
    category_id: uuid.UUID
    expires_at: datetime | None
    is_active: bool
    views_count: int
    clicks_count: int
    success_count: int
    fail_count: int
    last_verified_at: datetime | None
    # How much evidence backs this coupon (unverified/pending/verified/disputed/failed).
    # Exposed so the UI can show an honest trust state; the raw quality_score stays
    # private because it feeds ranking, including a revenue component.
    verification_status: VerificationStatus
    # When the offer's source was last read/checked (as opposed to
    # last_verified_at, which is a shopper vote). This is what a card shows as
    # "source checked on <date>".
    last_checked_at: datetime | None = None
    # Provider/staff-claimed conditions, kept verbatim: minimum order, terms,
    # eligibility, and (for hand-researched offers) the source page it was read
    # from. Public by design - it is the store's own stated conditions, which is
    # exactly what a shopper needs before trying a code.
    restrictions: dict[str, Any] | None = None
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def success_rate(self) -> float | None:
        return compute_success_rate(self.success_count, self.fail_count)


class VerifyRequest(BaseModel):
    worked: bool
    # Optional context from the reporter. Capped at 280 chars because it is
    # shown verbatim on the coupon page, so it must not become a message board.
    note: str | None = Field(default=None, max_length=280)


class VerifyResponse(BaseModel):
    success_count: int
    fail_count: int
    last_verified_at: datetime | None
    success_rate: float | None


class VerificationHistoryItem(BaseModel):
    """One community vote.

    Deliberately excludes ip_hash: visitors get the aggregate signal
    ("12 people, 10 worked") without any per-voter identifier leaving the server.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    worked: bool
    created_at: datetime
    note: str | None = None
