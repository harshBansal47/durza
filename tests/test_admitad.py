from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx

from app.ingestion.credentials import SourceCredentials
from app.ingestion.sources.admitad import AdmitadSource
from app.services.conversion_sync import AdmitadTransactionsClient


def _credentials() -> SourceCredentials:
    return SourceCredentials(
        values={
            "ADMITAD_CLIENT_ID": "client-id",
            "ADMITAD_CLIENT_SECRET": "client-secret",
        },
        names=("ADMITAD_CLIENT_ID", "ADMITAD_CLIENT_SECRET"),
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_admitad_source_authenticates_and_maps_coupon():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/token/":
            assert request.headers["Authorization"].startswith("Basic ")
            assert parse_qs(request.content.decode())["grant_type"] == ["client_credentials"]
            return httpx.Response(200, json={"access_token": "access-token"})
        assert request.url.path == "/coupons/website/website-1/"
        assert request.headers["Authorization"] == "Bearer access-token"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 41,
                        "name": "Twenty percent off",
                        "description": "Online purchase",
                        "promo_code": "save20",
                        "discount": "20%",
                        "goto_link": "https://tracked.example/click",
                        "date_end": "2026-12-31",
                        "campaign": {
                            "name": "Example Shop",
                            "site_url": "https://shop.example",
                        },
                    }
                ]
            },
        )

    async with _client(handler) as client:
        offers = await AdmitadSource(client=client).fetch(
            {"website_id": "website-1", "page_size": 5},
            credentials=_credentials(),
        )

    assert len(requests) == 2
    assert len(offers) == 1
    offer = offers[0]
    assert offer.title == "Twenty percent off"
    assert offer.code == "SAVE20"
    assert offer.discount_type == "percentage"
    assert offer.discount_value == 20.0
    assert offer.destination_url == "https://tracked.example/click"
    assert offer.store_name == "Example Shop"
    assert offer.external_id == "41"


async def test_admitad_source_uses_offset_pagination():
    coupon_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token/":
            return httpx.Response(200, json={"access_token": "access-token"})
        coupon_requests.append(int(request.url.params["offset"]))
        offset = int(request.url.params["offset"])
        if offset < 2:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": offset + 1,
                            "name": "Coupon",
                            "goto_link": f"https://tracked.example/{offset}",
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"results": []})

    async with _client(handler) as client:
        offers = await AdmitadSource(client=client).fetch(
            {"website_id": "website-1", "page_size": 1},
            credentials=_credentials(),
        )

    assert coupon_requests == [0, 1, 2]
    assert len(offers) == 2


def test_admitad_transaction_normalisation():
    client = AdmitadTransactionsClient(client_id="id", client_secret="secret")
    txn = client._normalise(
        {
            "id": 987,
            "subid": "clickref-123",
            "status": "approved",
            "order_sum": {"amount": "2000.00", "currency": "INR"},
            "payment": {"amount": "80.00", "currency": "INR"},
            "action_date": "2026-10-01T10:00:00Z",
            "approval_date": "2026-10-03T10:00:00Z",
        }
    )

    assert txn is not None
    assert txn.network == "admitad"
    assert txn.network_txn_id == "987"
    assert txn.clickref == "clickref-123"
    assert txn.raw_status == "approved"
    assert str(txn.sale_amount) == "2000.00"
    assert str(txn.commission_amount) == "80.00"
    assert txn.currency == "INR"
    assert txn.transaction_at == datetime(2026, 10, 1, 10, tzinfo=UTC)
    assert txn.validated_at == datetime(2026, 10, 3, 10, tzinfo=UTC)


def test_admitad_pagination_uses_total_count_when_page_is_short():
    from app.integrations.admitad import page_results

    rows, has_next = page_results(
        {"results": [{"id": "one"}], "_meta": {"count": 2}},
        offset=0,
        limit=10,
        resource="coupons",
    )

    assert len(rows) == 1
    assert has_next is True


async def test_admitad_transaction_client_fetches_actions():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token/":
            return httpx.Response(200, json={"access_token": "access-token"})
        requests.append(request)
        assert request.url.path == "/statistics/actions/"
        assert request.headers["Authorization"] == "Bearer access-token"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "action-1",
                        "subid1": "clickref-1",
                        "status": "pending",
                        "order_sum": "1000",
                        "payment": "40",
                        "currency": "INR",
                        "action_date": "2026-10-01T10:00:00Z",
                    }
                ]
            },
        )

    async with _client(handler) as client:
        transactions = await AdmitadTransactionsClient(
            client_id="client-id", client_secret="client-secret", client=client
        ).fetch(since=datetime(2026, 9, 1, tzinfo=UTC))

    assert len(requests) == 1
    assert requests[0].url.params["date_start"] == "2026-09-01"
    assert len(transactions) == 1
    assert transactions[0].network_txn_id == "action-1"
    assert transactions[0].clickref == "clickref-1"
    assert str(transactions[0].commission_amount) == "40"
