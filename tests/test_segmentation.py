from app.services.segmentation import compute_segment


def test_same_inputs_produce_the_same_segment():
    scores = {"category:electronics": 0.9, "store:nike": 0.4}
    assert compute_segment(scores) == compute_segment(dict(scores))
    assert compute_segment(scores) == "quick_buyer"


def test_threshold_boundary_is_exclusive():
    assert compute_segment({"category:electronics": 0.7}) == "researcher"
    assert compute_segment({"category:electronics": 0.7001}) == "quick_buyer"


def test_bank_signal_wins_over_fast_engagement():
    assert compute_segment({"bank:hdfc": 0.1, "category:electronics": 0.99}) == "savings_maximizer"


def test_three_store_signals_become_loyalist():
    scores = {"store:nike": 0.5, "store:adidas": 0.4, "store:puma": 0.3}
    assert compute_segment(scores) == "loyalist"
