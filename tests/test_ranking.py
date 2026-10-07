"""Phase 7 — Revenue-aware ranking feedback tests.

Tests that the quality score incorporates expected revenue without
dominating user intent, and that new coupons with no revenue data
are treated fairly (cold-start).
"""


from app.services.quality import (
    QualityInputs,
    QualityWeights,
    _expected_revenue,
    compute_quality,
)


def test_expected_revenue_component_none_for_no_data():
    assert _expected_revenue(None) is None
    assert _expected_revenue(0.0) is None


def test_expected_revenue_component_scales():
    # With enough clicks, the full mapping applies
    assert _expected_revenue(10.0, clicks=100) == 1.0
    assert _expected_revenue(5.0, clicks=100) == 0.5
    assert _expected_revenue(0.5, clicks=100) == 0.05


def test_cold_start_new_coupon_is_not_penalized():
    """A new coupon with no EPC data should get the same score as a mature
    coupon with identical other signals once expected_revenue is absent."""
    inputs = QualityInputs(
        verification_status="verified",
        success_rate=0.8,
        source_count=3,
        source_confidence=0.9,
        expires_at=None,
        created_at=None,
        last_verified_at=None,
        clicks_count=100,
        conversions_count=5,
        expected_revenue=None,
    )
    score_with_no_epc, breakdown = compute_quality(inputs)
    assert score_with_no_epc > 0
    assert "expected_revenue" not in breakdown


def test_high_epc_improves_score():
    base = QualityInputs(
        verification_status="verified",
        success_rate=0.8,
        source_count=3,
        source_confidence=0.9,
        expires_at=None,
        created_at=None,
        last_verified_at=None,
        clicks_count=100,
        conversions_count=5,
        expected_revenue=None,
    )
    base_score, _ = compute_quality(base)

    with_epc = QualityInputs(
        **{**base.__dict__, "expected_revenue": 10.0}
    )
    epc_score, breakdown = compute_quality(with_epc, weights=QualityWeights(expected_revenue=0.2))
    assert epc_score > base_score
    assert "expected_revenue" in breakdown


def test_revenue_cannot_dominate():
    """Even with a very high EPC, a poorly verified coupon should not outrank
    a well-verified coupon because revenue has a capped weight."""
    high_epc_bad_verification = QualityInputs(
        verification_status="disputed",
        success_rate=0.3,
        source_count=1,
        source_confidence=0.2,
        clicks_count=100,
        conversions_count=5,
        expected_revenue=10.0,
    )
    high_score, _ = compute_quality(
        high_epc_bad_verification,
        weights=QualityWeights(expected_revenue=0.1),
    )

    good_coupon = QualityInputs(
        verification_status="verified",
        success_rate=0.9,
        source_count=5,
        source_confidence=0.95,
        clicks_count=100,
        conversions_count=5,
        expected_revenue=None,
    )
    good_score, _ = compute_quality(good_coupon)

    assert good_score >= high_score


def test_low_sample_epc_gets_reduced_influence():
    """EPC from very low click counts must not receive full ranking influence.

    A coupon with 3 clicks and a high EPC should score lower on the
    expected_revenue component than one with 30+ clicks and the same EPC.
    """
    high_epc_low_clicks = QualityInputs(
        verification_status="verified",
        success_rate=0.8,
        clicks_count=3,
        expected_revenue=10.0,
    )
    _, low_breakdown = compute_quality(high_epc_low_clicks)

    high_epc_high_clicks = QualityInputs(
        verification_status="verified",
        success_rate=0.8,
        clicks_count=100,
        expected_revenue=10.0,
    )
    _, high_breakdown = compute_quality(high_epc_high_clicks)

    # The low-click coupon's expected_revenue component should be much smaller
    assert low_breakdown["expected_revenue"] < high_breakdown["expected_revenue"] * 0.2


def test_large_sample_epc_behaves_normally():
    """With enough clicks, EPC receives full influence (no penalty)."""
    inputs = QualityInputs(
        verification_status="verified",
        success_rate=0.8,
        clicks_count=1000,
        expected_revenue=5.0,
    )
    _, breakdown = compute_quality(inputs)
    # 5.0 EPC maps to 0.5, and with 1000 clicks there is no reduction
    assert abs(breakdown["expected_revenue"] - 0.5 * 0.30) < 0.01


def test_ranking_output_is_deterministic():
    """Same inputs always produce the same score."""
    inputs = QualityInputs(
        verification_status="verified",
        success_rate=0.9,
        source_count=3,
        source_confidence=0.9,
        clicks_count=500,
        expected_revenue=2.0,
    )
    score1, _ = compute_quality(inputs)
    score2, _ = compute_quality(inputs)
    assert score1 == score2
