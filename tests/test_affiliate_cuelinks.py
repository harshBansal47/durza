"""Cuelinks link wrapping: publisher-id wrapper + clickref as subid1."""

from urllib.parse import parse_qs, urlparse

import pytest

from app.core.config import get_settings
from app.services import affiliate


@pytest.fixture
def cuelinks_publisher(monkeypatch):
    monkeypatch.setenv("CUELINKS_PUBLISHER_ID", "pub-42")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_cuelinks_url_is_wrapped_with_publisher_id(cuelinks_publisher):
    url = affiliate.build_outbound_url(
        "https://www.myntra.com/kollabs", network="cuelinks", link_template=None, clickref="cr1"
    )
    assert url.startswith("https://go.cuelinks.com/ald?")
    outer = parse_qs(urlparse(url).query)
    assert outer["pid"] == ["pub-42"]
    inner = outer["url"][0]
    inner_qs = parse_qs(urlparse(inner).query)
    assert inner_qs["subid1"] == ["cr1"]


def test_cuelinks_without_publisher_id_falls_back_to_subid1(monkeypatch):
    monkeypatch.setenv("CUELINKS_PUBLISHER_ID", "")
    get_settings.cache_clear()
    try:
        url = affiliate.build_outbound_url(
            "https://www.myntra.com/kollabs", network="cuelinks", link_template=None, clickref="cr1"
        )
    finally:
        get_settings.cache_clear()
    assert url.startswith("https://www.myntra.com/kollabs")
    assert parse_qs(urlparse(url).query)["subid1"] == ["cr1"]


def test_non_cuelinks_networks_keep_direct_wrapping(cuelinks_publisher):
    url = affiliate.build_outbound_url(
        "https://www.example.com/p", network="awin", link_template=None, clickref="cr9"
    )
    assert url.startswith("https://www.example.com/p")
    assert parse_qs(urlparse(url).query)["clickref"] == ["cr9"]
