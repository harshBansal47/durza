"""Rule-based, versioned personalization segments."""

from __future__ import annotations


def compute_segment(scores: dict[str, float]) -> str:
    keys = list(scores)
    if any(k.startswith("bank:") for k in keys):
        return "savings_maximizer"
    if any(k.startswith("store:") for k in keys) and len(keys) >= 3:
        return "loyalist"
    if any("trust" in k for k in keys):
        return "trust_seeker"
    if scores and max(scores.values()) > 0.7:
        return "quick_buyer"
    return "researcher"
