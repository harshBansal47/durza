"""Tests for the JSON feed adapter, retry helper, and probe retry behaviour."""

from typing import Any

import httpx
import pytest

from app.ingestion import probe
from app.ingestion.retry import with_retries
from app.ingestion.sources.feed import FeedSource

pytestmark = pytest.mark.asyncio


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_feed_source_parses_list_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "title": "Feed Deal",
                    "store_slug": "ajio",
                    "category_slug": "fashion",
                    "discount_text": "30% off",
                    "destination_url": "https://www.ajio.com/x",
                }
            ],
        )

    source = FeedSource(client=_mock_client(handler))
    offers = await source.fetch({"url": "https://feed.example.com/offers.json"})
    assert len(offers) == 1
    assert offers[0].title == "Feed Deal"


async def test_feed_source_parses_wrapped_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"offers": [{"title": "A", "destination_url": "https://x.co"}]}
        )

    source = FeedSource(client=_mock_client(handler))
    offers = await source.fetch({"url": "https://feed.example.com/offers.json"})
    assert len(offers) == 1


async def test_feed_source_requires_url():
    with pytest.raises(ValueError):
        await FeedSource().fetch({})


async def test_feed_source_retries_transient_failure(monkeypatch):
    calls = {"n": 0}

    async def _no_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", _no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500)
        return httpx.Response(200, json=[])

    source = FeedSource(client=_mock_client(handler))
    offers = await source.fetch({"url": "https://feed.example.com/x.json"})
    assert offers == []
    assert calls["n"] == 3


async def test_with_retries_exhausts_and_raises():
    attempts = {"n": 0}

    async def _always_fail():
        attempts["n"] += 1
        raise ConnectionError("boom")

    with pytest.raises(ConnectionError):
        await with_retries(_always_fail, attempts=3, base_delay=0, retryable=(ConnectionError,))
    assert attempts["n"] == 3


async def test_probe_retries_5xx_then_succeeds(monkeypatch):
    async def _no_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500 if calls["n"] < 2 else 200)

    # Patch the AsyncClient constructor used inside probe_destination.
    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    ok, reason = await probe.probe_destination("https://example.com/deal")
    assert ok is True
    assert reason is None
    assert calls["n"] == 2


async def test_probe_gives_up_after_retries(monkeypatch):
    async def _no_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    ok, reason = await probe.probe_destination("https://example.com/dead")
    assert ok is False
    assert reason is not None
    assert calls["n"] == 3


def _awin_credentials(key: str = "test_key"):
    """The Awin API key, resolved the way the pipeline resolves it.

    Not passed in `config`. The key is no longer a column value in
    `sources.config` — it comes from the environment via `Source.credential_ref`
    and arrives as a `SourceCredentials`. These tests assert the adapter's
    *behaviour*, so the credential has to arrive the way production delivers it.
    """
    from app.ingestion.credentials import SourceCredentials

    return SourceCredentials(values={"AWIN_API_KEY": key}, names=("AWIN_API_KEY",))


# ---- Awin adapter ----
#
# The Awin adapter is the first real affiliate network integration. It must
# handle pagination, auth, field mapping, and error resilience. These tests
# verify the adapter produces well-formed RawOffers that the normalizer accepts.

import httpx
import pytest

from app.ingestion.sources.awin import AwinSource


@pytest.mark.asyncio
async def test_awin_source_maps_voucher_to_raw_offer():
    # A minimal voucher payload that exercises the happy path.
    voucher = {
        "id": "12345",
        "title": "20% off everything",
        "description": "Site-wide discount",
        "voucherCode": "SAVE20",
        "deepLink": "https://store.awin.com/click?ref=123",
        "endDate": "2025-12-31",
        "discountType": "percentage",
        "discountPercentage": 20.0,
        "advertiserName": "Test Store",
        "advertiserId": 999,
        "advertiserUrl": "https://teststore.com",
    }
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/publishers/123/vouchers"
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"results": [voucher]})
        return httpx.Response(200, json={"results": []})

    source = AwinSource(client=_mock_client(handler))
    offers = await source.fetch(
        {
            "publisher_id": 123,
            "deal_types": ["coupon"],
        },
        credentials=_awin_credentials(),
    )

    assert len(offers) == 1
    offer = offers[0]
    assert offer.title == "20% off everything"
    assert offer.code == "SAVE20"
    assert offer.destination_url == "https://store.awin.com/click?ref=123"
    assert offer.discount_type == "percentage"
    assert offer.discount_value == 20.0
    assert offer.store_slug == "test-store"
    assert offer.store_name == "Test Store"
    assert offer.external_id == "12345"


@pytest.mark.asyncio
async def test_awin_source_skips_voucher_without_code():
    # Vouchers without codes are not actionable for our coupon model.
    voucher = {
        "id": "12346",
        "title": "Free shipping",
        "voucherCode": None,
        "deepLink": "https://store.awin.com/click?ref=456",
    }
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"results": [voucher]})
        return httpx.Response(200, json={"results": []})

    source = AwinSource(client=_mock_client(handler))
    offers = await source.fetch(
        {
            "publisher_id": 123,
            "deal_types": ["coupon"],
        },
        credentials=_awin_credentials(),
    )

    assert offers == []


@pytest.mark.asyncio
async def test_awin_source_maps_deal_to_raw_offer():
    # Deals have no code, just a deep link.
    deal = {
        "id": "12347",
        "title": "Up to 50% off sale",
        "description": "Seasonal sale",
        "deepLink": "https://store.awin.com/sale?ref=789",
        "endDate": "2025-11-30",
        # Awin's deal payload carries one `dealType`; the literal used to declare
        # it twice, so the earlier value was dead weight. The adapter reads this
        # field as the discount type and the assertions below expect
        # "percentage", so that is the value that survives.
        "dealType": "percentage",
        "discountPercentage": 50.0,
        "advertiserName": "Sale Store",
    }
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/deals" in str(request.url)
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"results": [deal]})
        return httpx.Response(200, json={"results": []})

    source = AwinSource(client=_mock_client(handler))
    offers = await source.fetch(
        {
            "publisher_id": 123,
            "deal_types": ["sale"],
        },
        credentials=_awin_credentials(),
    )

    assert len(offers) == 1
    offer = offers[0]
    assert offer.title == "Up to 50% off sale"
    assert offer.code is None
    assert offer.destination_url == "https://store.awin.com/sale?ref=789"
    assert offer.discount_type == "percentage"
    assert offer.discount_value == 50.0


@pytest.mark.asyncio
async def test_awin_source_paginates_through_multiple_pages():
    # The adapter should keep fetching until an empty page.
    page1 = [
        {
            "id": str(i),
            "title": f"Deal {i}",
            "voucherCode": f"CODE{i}",
            "deepLink": f"https://x/{i}",
        }
        for i in range(3)
    ]
    page2 = [{"id": "3", "title": "Deal 3", "voucherCode": "CODE3", "deepLink": "https://x/3"}]
    page3: list[dict] = []

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"results": page1})
        if calls["n"] == 2:
            return httpx.Response(200, json={"results": page2})
        return httpx.Response(200, json={"results": page3})

    source = AwinSource(client=_mock_client(handler))
    offers = await source.fetch(
        {
            "publisher_id": 123,
            "deal_types": ["coupon"],
            "page_size": 2,  # force pagination
        },
        credentials=_awin_credentials(),
    )

    assert len(offers) == 4
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_awin_source_filters_by_currency_and_region():
    # The adapter should pass currency and regions as query params.
    captured: dict[str, Any] = {}
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        captured["params"] = dict(request.url.params)
        if calls["n"] == 1:
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"results": []})

    source = AwinSource(client=_mock_client(handler))
    await source.fetch(
        {
            "publisher_id": 123,
            "currency": "GBP",
            "regions": ["GB", "IE"],
        },
        credentials=_awin_credentials(),
    )

    assert captured["params"]["currency"] == "GBP"
    assert captured["params"]["regions"] == "GB,IE"


@pytest.mark.asyncio
async def test_awin_source_requires_credentials():
    """Both halves of the requirement, refused before a request is made.

    The API key comes from the environment via `credential_ref`, so "no key" means
    `credentials` is `None` or resolved to nothing — not that `config` lacks an
    `api_key` key. `publisher_id` is the other half: it is not a secret, so it
    legitimately stays in `config`.

    `calls == 0` is the assertion that matters. The failure mode being guarded
    against is a request going out with `Authorization: Bearer None`, which returns
    a 401 that reads like a revoked key rather than a missing configuration.
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"results": []})

    source = AwinSource(client=_mock_client(handler))

    # No credential at all.
    with pytest.raises(ValueError, match="requires an API key"):
        await source.fetch({"publisher_id": 123})

    from app.ingestion.credentials import SourceCredentials

    # A credential that resolved to nothing.
    with pytest.raises(ValueError, match="requires an API key"):
        await source.fetch({"publisher_id": 123}, credentials=SourceCredentials())

    # Credential present, publisher_id missing.
    with pytest.raises(ValueError, match="requires 'publisher_id'"):
        await source.fetch({}, credentials=_awin_credentials())

    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_awin_source_sends_the_resolved_credential():
    """The key reaches Awin as a bearer token, and only from `credentials`.

    Asserts the credential is not read out of `config` as well, because that is
    the regression this replaced: a key in a JSONB column is a key in every
    backup of that table.
    """
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json={"results": []})

    source = AwinSource(client=_mock_client(handler))
    await source.fetch(
        {"publisher_id": 123, "api_key": "leaked-from-config"},
        credentials=_awin_credentials("real-key"),
    )

    assert seen["auth"] == "Bearer real-key"
    assert "leaked-from-config" not in seen["auth"]


# Helper reused from existing tests
def _mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))
