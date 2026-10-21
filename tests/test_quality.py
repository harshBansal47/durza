from app.services.quality import QualityInputs, QualityWeights, compute_quality


def test_community_success_uses_report_counts():
    # 1 report + 500 clicks cannot outrank 94% from 48 reports
    low, _ = compute_quality(
        QualityInputs(success_rate=1.0, success_count=1, fail_count=0, clicks_count=500)
    )
    high, _ = compute_quality(
        QualityInputs(success_rate=0.94, success_count=45, fail_count=3, clicks_count=10)
    )
    assert high > low


def test_weight_validation():
    w = QualityWeights(verification_status=0.45, community_success=0.25, expected_revenue=0.30)
    assert w.validated().normalized()["expected_revenue"] <= 0.4
