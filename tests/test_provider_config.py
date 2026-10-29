"""The committed provider list must be usable: no duplicates, no kind without an adapter."""

from app.ingestion.provider_config import PROVIDERS
from app.ingestion.registry import get_adapter


def test_every_configured_provider_has_an_adapter() -> None:
    for spec in PROVIDERS:
        get_adapter(spec.kind)


def test_provider_slugs_and_names_are_unique() -> None:
    assert len({spec.slug for spec in PROVIDERS}) == len(PROVIDERS)
    assert len({spec.name for spec in PROVIDERS}) == len(PROVIDERS)
