"""Cheap structural checks before an offer is allowed near the database."""

from urllib.parse import urlparse

from app.ingestion.normalize import NormalizedOffer


def validate_offer(offer: NormalizedOffer) -> list[str]:
    errors: list[str] = []
    if not offer.destination_url.startswith(("http://", "https://")):
        errors.append("destination_url is not http(s)")
    else:
        host = urlparse(offer.destination_url).netloc
        if not host or "." not in host:
            errors.append("destination_url has no valid host")
    if offer.discount_value is not None and offer.discount_value < 0:
        errors.append("negative discount_value")
    if (
        offer.discount_type.value == "percentage"
        and offer.discount_value is not None
        and not (0 < offer.discount_value <= 100)
    ):
        errors.append("percentage discount out of range 0-100")
    if offer.code is not None and len(offer.code) > 50:
        errors.append("code longer than 50 chars")
    return errors
