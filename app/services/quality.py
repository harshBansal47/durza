"""The ranking score behind the "Recommended" sort.

A coupon site ranks by how much the shopper is likely to benefit, not by how
loud an affiliate network is about an offer. Those come apart: the highest
commission offer is frequently the one with the shortest remaining validity and
the worst community track record.

The formula is a weighted sum of inputs that are each independently defensible,
and it is **deliberately not clever**. There is no learned model, no feature
interaction, and no attempt to be clever about which inputs multiply. Three
reasons:

* It has to be explainable. When a coupon ranks below another and someone asks
  why, the answer has to be a sentence, not a SHAP plot.
* It has to be auditable. The weights are settings, so a ranking change is a
  reviewed config diff rather than a silent behaviour change in a rebuild.
* It has to be cheap. This runs on every coupon in a listing query's worth of
  rows. A score that needs a subquery per component does not belong here.

Each component returns 0..1 and the weights are in `QualityWeights`. All the
weights sum to 1.0 so the result is directly readable as a percentage, which is
what makes a score of 0.62 mean something to a human.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import get_settings
from app.models.coupon import Coupon, VerificationStatus

#: A coupon expiring sooner is *more* urgent, not less. Past this point the
#: urgency signal inverts and the coupon starts costing trust by being on screen.
URGENT_WINDOW = timedelta(days=3)
STALE_WINDOW = timedelta(days=14)


@dataclass(frozen=True)
class QualityInputs:
    """Everything the score is computed from. Flat, typed, and optional.

    Every field is optional because they come from different places: some from
    the coupon row, some from aggregated click/conversion history, some from the
    verification sweep. An input that is genuinely unavailable scores `None` and
    its weight is redistributed, so a missing measurement never silently reads
    as a zero.
    """

    verification_status: VerificationStatus = VerificationStatus.unverified
    #: 0..1 community success rate. None when nobody has reported.
    success_rate: float | None = None
    #: How many distinct sources currently back this coupon.
    source_count: int = 1
    #: 0..1 source confidence of the winning claim.
    source_confidence: float = 0.5
    expires_at: datetime | None = None
    created_at: datetime | None = None
    #: When the coupon was last confirmed active by a checker.
    last_verified_at: datetime | None = None
    #: Lifetime outbound clicks.
    clicks_count: int = 0
    #: Confirmed affiliate conversions attributed to this coupon.
    conversions_count: int = 0
    #: 0..1 merchant popularity, if the caller has precomputed it.
    merchant_popularity: float | None = None
    discount_value: float | None = None
    discount_type: str = "deal"
    #: True for a code-less deal. Such offers convert differently and, in
    #: aggregate, more reliably — a code the shopper can mistype is a code that
    #: produces a support ticket.
    is_deal_without_code: bool = False
    #: Expected profit per click (EPC). None when insufficient data exists.
    #: This is the monetizable value of an impression at this coupon's
    #: historical click. When present, a small portion of the ranking weight
    #: shifts to rewarding coupons that actually earn money, but never at
    #: the expense of user intent or coupon quality.
    expected_revenue: float | None = None


@dataclass(frozen=True)
class QualityWeights:
    """Component weights. Trust and EPC split 70/30.

    verification_status + community_success (the shopper-vote signals) carry
    70%, and expected_revenue — normalized 30-day EPC — carries 30%. Every
    other legacy component is zero-weighted: the recommendation sort optimises
    for coupons the community trusts that also make us money, nothing else
    creeps in.
    """

    verification_status: float = 0.45
    community_success: float = 0.25
    source_confidence: float = 0.0
    multi_source: float = 0.0
    recency: float = 0.0
    expiry_urgency: float = 0.0
    conversion_rate: float = 0.0
    merchant_popularity: float = 0.0
    # Hybrid ranking: 70% trust (verification + shopper votes, above), 30%
    # normalized EPC. The old default of 0.0 meant the revenue tables could
    # fill forever and the ranking would never notice.
    expected_revenue: float = 0.30

    def normalized(self) -> dict[str, float]:
        """Weights scaled to sum to 1, so an edited setting cannot skew the range."""
        total = sum(
            [
                self.verification_status,
                self.community_success,
                self.source_confidence,
                self.multi_source,
                self.recency,
                self.expiry_urgency,
                self.conversion_rate,
                self.merchant_popularity,
                self.expected_revenue,
            ]
        )
        if total <= 0:
            raise ValueError("quality weights must sum to a positive number")
        return {
            "verification_status": self.verification_status / total,
            "community_success": self.community_success / total,
            "source_confidence": self.source_confidence / total,
            "multi_source": self.multi_source / total,
            "recency": self.recency / total,
            "expiry_urgency": self.expiry_urgency / total,
            "conversion_rate": self.conversion_rate / total,
            "merchant_popularity": self.merchant_popularity / total,
            "expected_revenue": self.expected_revenue / total,
        }


#: Status -> 0..1. `unverified` and `pending` score equal and low on purpose. A
#: fresh coupon and a coupon nobody has got to are the same epistemic state, and
#: scoring them differently would make "Recommended" a proxy for how long a
#: coupon has been on the site.
_VERIFICATION_SCORES: dict[VerificationStatus, float] = {
    VerificationStatus.verified: 1.0,
    VerificationStatus.unverified: 0.4,
    VerificationStatus.pending: 0.4,
    VerificationStatus.disputed: 0.2,
    VerificationStatus.failed: 0.0,
}


def _verification_score(status: VerificationStatus) -> float:
    return _VERIFICATION_SCORES.get(status, 0.4)


def _community_success(success_rate: float | None, *, samples: int) -> float | None:
    """Confidence-adjusted success rate.

    A single "it worked" report is 100% and means nothing, so the raw rate is
    pulled toward 0.5 by sample size. With ten reports the adjustment is nearly
    gone, which is the point: the number only becomes evidence as it accumulates.

    `samples` is passed in rather than inferred because `success_rate` is a
    precomputed field and the counts live elsewhere; threading them together here
    keeps this function pure and therefore testable without a database.
    """
    if success_rate is None:
        return None
    # 10 reports removes ~86% of the pull toward the prior.
    shrinkage = samples / (samples + 10) if samples > 0 else 0.0
    return 0.5 + (success_rate - 0.5) * shrinkage


def _recency(
    created_at: datetime | None, last_verified_at: datetime | None, now: datetime
) -> float | None:
    """1.0 for something confirmed in the last day, decaying to 0 at 30 days.

    Uses `last_verified_at` in preference to `created_at`: a coupon created six
    months ago and re-confirmed this morning is fresh information, and ranking
    it by creation date would bury exactly the coupons worth showing.
    """
    reference = last_verified_at or created_at
    if reference is None:
        return None
    age_days = max(0.0, (now - reference).total_seconds() / 86400)
    if age_days <= 1:
        return 1.0
    if age_days >= 30:
        return 0.0
    return 1.0 - (age_days - 1) / 29.0


def _expiry_urgency(expires_at: datetime | None, now: datetime) -> float | None:
    """1.0 when expiring within `URGENT_WINDOW`, dropping to 0 with 14 days left.

    Past expiry this returns None rather than a negative number: an expired
    coupon is not low-priority, it is not eligible, and encoding that as a low
    score would let it outrank something merely unverified if the sort were ever
    loosened.
    """
    if expires_at is None:
        return None
    remaining = expires_at - now
    if remaining.total_seconds() <= 0:
        return None
    days = remaining.total_seconds() / 86400
    if days <= URGENT_WINDOW.days:
        return 1.0
    if days >= STALE_WINDOW.days:
        return 0.0
    return 1.0 - (days - URGENT_WINDOW.days) / (STALE_WINDOW.days - URGENT_WINDOW.days)


def _conversion_rate(clicks: int, conversions: int) -> float | None:
    """Conversions per click, mapped onto 0..1.

    Capped at a 5% click-to-sale rate. Affiliate conversion rates are low
    single-digit percentages; scaling linearly to a hypothetical 100% would put
    every realistic coupon in the bottom 5% of the range and make the term
    inert.
    """
    if clicks <= 0:
        return None
    return min(1.0, (conversions / clicks) / 0.05)


def _merchant_popularity(value: float | None) -> float | None:
    return None if value is None else max(0.0, min(1.0, value))


#: Minimum clicks before EPC receives full ranking influence. Below this,
#: the EPC signal is scaled linearly toward zero, so a single lucky sale on
#: a 2-click coupon cannot outrank a proven earner with thousands of clicks.
#: This is the simplest statistically robust guard: it does not invent a
#: prior, it just refuses to trust small samples.
MIN_EPC_CLICKS = 30


def _expected_revenue(value: float | None, *, clicks: int = 0) -> float | None:
    """Map expected revenue per click onto 0..1.

    A cap of ₹10 per click → 1.0. Below ₹1 → near zero. When None
    (insufficient data), returns None so its weight is redistributed.

    Low-sample protection: when `clicks` is below MIN_EPC_CLICKS, the
    mapped value is scaled by `clicks / MIN_EPC_CLICKS`. A coupon with
    15 clicks gets half the influence; one with 3 clicks gets a tenth.
    This prevents a single high-value sale on a barely-trafficked coupon
    from dominating the ranking.
    """
    if value is None or value <= 0:
        return None
    mapped = min(1.0, value / 10.0)
    if clicks < MIN_EPC_CLICKS:
        mapped *= clicks / MIN_EPC_CLICKS
    return mapped


def compute_quality(
    inputs: QualityInputs,
    *,
    weights: QualityWeights | None = None,
    now: datetime | None = None,
) -> tuple[int, dict[str, float]]:
    """The score (0..100) and its per-component breakdown.

    Returning the breakdown rather than just the number is what makes the ranking
    reviewable — the admin panel shows it, and a regression from 72 to 40 is then
    attributable to a component rather than being a mystery.

    Missing components have their weight redistributed across the ones that are
    present. A coupon with no expiry and no clicks is not penalised for having
    no history; it is scored on what is actually known about it.
    """
    weights = (weights or QualityWeights()).normalized()
    now = now or datetime.now(UTC)

    samples = inputs.clicks_count if inputs.success_rate is not None else 0
    components: dict[str, float | None] = {
        "verification_status": _verification_score(inputs.verification_status),
        "community_success": _community_success(inputs.success_rate, samples=samples),
        "source_confidence": max(0.0, min(1.0, inputs.source_confidence)),
        "multi_source": min(1.0, max(0, inputs.source_count - 1) / 2.0),
        "recency": _recency(inputs.created_at, inputs.last_verified_at, now),
        "expiry_urgency": _expiry_urgency(inputs.expires_at, now),
        "conversion_rate": _conversion_rate(inputs.clicks_count, inputs.conversions_count),
        "merchant_popularity": _merchant_popularity(inputs.merchant_popularity),
        "expected_revenue": _expected_revenue(inputs.expected_revenue, clicks=inputs.clicks_count),
    }

    present = {name: value for name, value in components.items() if value is not None}
    if not present:
        return 0, {}

    total_weight = sum(weights[name] for name in present)
    if total_weight <= 0:
        return 0, {name: 0.0 for name in present}

    breakdown = {
        name: round(value * weights[name] / total_weight, 4) for name, value in present.items()
    }
    score = round(sum(breakdown.values()) * 100)
    return max(0, min(100, score)), breakdown


def score_for_coupon(
    coupon: Coupon,
    *,
    conversions_count: int = 0,
    merchant_popularity: float | None = None,
    epc: float | None = None,
    weights: QualityWeights | None = None,
    now: datetime | None = None,
) -> int:
    """Convenience wrapper for the common case: score straight off a Coupon row."""
    now = now or datetime.now(UTC)
    total = coupon.success_count + coupon.fail_count
    inputs = QualityInputs(
        verification_status=coupon.verification_status,
        success_rate=(coupon.success_count / total) if total else None,
        source_count=coupon.source_count or 1,
        source_confidence=0.5,
        expires_at=_aware(coupon.expires_at),
        created_at=_aware(coupon.created_at),
        last_verified_at=_aware(coupon.last_verified_at),
        clicks_count=coupon.clicks_count,
        conversions_count=conversions_count,
        merchant_popularity=merchant_popularity,
        discount_value=coupon.discount_value,
        discount_type=coupon.discount_type.value,
        is_deal_without_code=not coupon.code,
        expected_revenue=epc,
    )
    score, _ = compute_quality(inputs, weights=weights, now=now)
    return score


def _aware(value: datetime | None) -> datetime | None:
    """SQLite round-trips `timestamptz` as naive. Compare like with like."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def weights_from_settings(settings: Any = None) -> QualityWeights:
    """Weights, overridable by environment.

    `extra="ignore"` in Settings means an operator can set
    `QUALITY_WEIGHT_VERIFICATION_STATUS=0.4` without the rest being spelled out.
    Defaults come from the dataclass, so the ranking is fully specified even when
    nothing is configured.
    """
    settings = settings or get_settings()
    defaults = QualityWeights()
    return QualityWeights(
        verification_status=getattr(
            settings, "quality_weight_verification_status", defaults.verification_status
        ),
        community_success=getattr(
            settings, "quality_weight_community_success", defaults.community_success
        ),
        source_confidence=getattr(
            settings, "quality_weight_source_confidence", defaults.source_confidence
        ),
        multi_source=getattr(settings, "quality_weight_multi_source", defaults.multi_source),
        recency=getattr(settings, "quality_weight_recency", defaults.recency),
        expiry_urgency=getattr(settings, "quality_weight_expiry_urgency", defaults.expiry_urgency),
        conversion_rate=getattr(
            settings, "quality_weight_conversion_rate", defaults.conversion_rate
        ),
        merchant_popularity=getattr(
            settings, "quality_weight_merchant_popularity", defaults.merchant_popularity
        ),
        expected_revenue=getattr(
            settings, "quality_weight_expected_revenue", defaults.expected_revenue
        ),
    )
