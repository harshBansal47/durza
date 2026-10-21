"""Anomaly detection for ingestion candidates."""

from __future__ import annotations

from app.core.config import get_settings
from app.ingestion.normalize import NormalizedOffer


def quarantine_reason(offer: NormalizedOffer, *, store_domain: str | None = None) -> str | None:
    mode = getattr(get_settings(), "ingestion_quarantine_mode", "shadow")
    if mode == "off":
        return None

    # Discount threshold by type.
    if offer.discount_type.value == "percentage" and offer.discount_value is not None and offer.discount_value > 90:
        return "discount_threshold"
    if offer.discount_type.value in {"flat", "up_to"} and offer.discount_value is not None and offer.discount_value > 10000:
        return "discount_threshold"

    # Destination domain vs store domain mismatch.
    if store_domain:
        from urllib.parse import urlparse
        host = urlparse(offer.destination_url).netloc.lower()
        if host and store_domain.lower() not in host and host not in store_domain.lower():
            return "merchant_domain_mismatch"

    return None
