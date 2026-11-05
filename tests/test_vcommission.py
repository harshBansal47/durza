import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from app.models.affiliate_campaign import AffiliateCampaign, AffiliateCampaignSync
from app.models.store import Store
from app.models.user import Role
from app.services import vcommission_service as service


def _campaign(
    *,
    external_id: str = "campaign-1",
    tracking_link: str = "https://track.example/offer?token=secret",
    title: str = "Summer offer",
) -> dict:
    return {
        "id": external_id,
        "title": title,
        "description": "<p>Earn <b>commission</b></p><script>do not keep this</script>",
        "kpi": "",
        "tracking_link": tracking_link,
        "thumbnail": "https://cdn.example/offer.jpg",
        "preview_url": "https://shop.example/offer",
        "merchant_name": "Example Shop",
        "model": "CPA",
        "currency": "inr",
        "payouts": [{"amount": 12}],
        "categories": ["Fashion"],
        "countries": ["in"],
        "active": True,
        "user_id": "private-publisher-id",
        "HidePayoutFromPubsList": ["private-publisher-id"],
    }


def _handler(campaigns: list[dict], *, pagination: dict | None = None):
    payload = {"success": True, "data": {"campaigns": campaigns}}
    if pagination is not None:
        payload["data"]["pagination"] = pagination

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return handle


async def _http_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_sync_normalizes_campaigns_and_is_idempotent(async_session_maker, monkeypatch):
    settings = service.get_settings()
    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: settings.model_copy(
            update={"vcommissions_campaigns_url": "https://api.example/campaigns"}
        ),
    )
    async with async_session_maker() as db, await _http_client(_handler([_campaign()])) as client:
        store = Store(
            name="Example Shop",
            slug="example-shop",
            normalized_name="example shop",
            normalized_domain="shop.example",
        )
        db.add(store)
        await db.commit()
        first = await service.sync_campaigns(db, client=client)
        second = await service.sync_campaigns(db, client=client)
        campaign = await db.scalar(select(AffiliateCampaign))
        syncs = list((await db.execute(select(AffiliateCampaignSync))).scalars().all())

    assert first.status == "success"
    assert first.inserted == 1
    assert second.unchanged == 1
    assert campaign is not None
    assert campaign.description == "Earn commission"
    assert campaign.kpi is None
    assert campaign.countries == ["IN"]
    assert campaign.currency == "INR"
    assert campaign.network == "vcommission"
    assert campaign.store_id == store.id
    assert "secret" not in str(campaign.network_metadata)
    assert "private-publisher-id" not in str(campaign.network_metadata)
    assert len(syncs) == 2
    assert syncs[-1].status == "success"


@pytest.mark.asyncio
async def test_partial_feed_does_not_deactivate_absent_campaigns(async_session_maker, monkeypatch):
    settings = service.get_settings()
    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: settings.model_copy(
            update={"vcommissions_campaigns_url": "https://api.example/campaigns"}
        ),
    )
    async with async_session_maker() as db:
        async with await _http_client(
            _handler([_campaign(), _campaign(external_id="campaign-2")])
        ) as client:
            await service.sync_campaigns(db, client=client)
        async with await _http_client(
            _handler([_campaign(title="Updated offer"), {"id": "missing-fields"}])
        ) as client:
            summary = await service.sync_campaigns(db, client=client)
        rows = list((await db.execute(select(AffiliateCampaign))).scalars().all())

    assert summary.status == "partial"
    assert summary.skipped == 1
    assert {row.external_campaign_id: row.is_active for row in rows} == {
        "campaign-1": True,
        "campaign-2": True,
    }


@pytest.mark.asyncio
async def test_empty_feed_preserves_existing_campaigns(async_session_maker, monkeypatch):
    settings = service.get_settings()
    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: settings.model_copy(
            update={"vcommissions_campaigns_url": "https://api.example/campaigns"}
        ),
    )
    async with async_session_maker() as db:
        async with await _http_client(_handler([_campaign()])) as client:
            await service.sync_campaigns(db, client=client)
        async with await _http_client(_handler([])) as client:
            summary = await service.sync_campaigns(db, client=client)
        campaign = await db.scalar(select(AffiliateCampaign))

    assert summary.status == "partial"
    assert summary.deactivated == 0
    assert campaign is not None and campaign.is_active


def test_next_page_must_be_same_origin():
    body = {
        "data": {
            "campaigns": [],
            "pagination": {"links": {"next": "https://evil.example/steal"}},
        }
    }
    with pytest.raises(service.VCommissionSyncError, match="cross-origin"):
        service._next_url(body, "https://api.example/campaigns")


@pytest.mark.asyncio
async def test_single_response_is_complete_when_count_matches_campaigns():
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "page": 1,
                    "count": 1,
                    "campaigns": [_campaign()],
                },
            },
        )

    async with await _http_client(handle) as client:
        campaigns, page_count = await service._fetch_campaigns(
            "https://api.example/campaigns", client=client
        )

    assert len(requests) == 1
    assert page_count == 1
    assert len(campaigns) == 1


@pytest.mark.asyncio
async def test_count_mismatch_prevents_syncing_an_incomplete_feed():
    async with await _http_client(
        lambda _request: httpx.Response(
            200,
            json={
                "success": True,
                "data": {"page": 1, "count": 57, "campaigns": [_campaign()]},
            },
        )
    ) as client:
        with pytest.raises(service.VCommissionSyncError, match="count does not match"):
            await service._fetch_campaigns("https://api.example/campaigns", client=client)


@pytest.mark.asyncio
async def test_fetch_follows_same_origin_pagination():
    def handle(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        next_page = {"pagination": {"links": {"next": "?page=2"}}} if page is None else {}
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"campaigns": [{"id": page or "1"}], **next_page},
            },
        )

    async with await _http_client(handle) as client:
        campaigns, page_count = await service._fetch_campaigns(
            "https://api.example/campaigns", client=client
        )

    assert page_count == 2
    assert campaigns == [{"id": "1"}, {"id": "2"}]


@pytest.mark.asyncio
async def test_failed_fetch_preserves_existing_campaigns_and_records_failure(
    async_session_maker, monkeypatch
):
    settings = service.get_settings()
    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: settings.model_copy(
            update={"vcommissions_campaigns_url": "https://api.example/campaigns"}
        ),
    )
    async with async_session_maker() as db:
        async with await _http_client(_handler([_campaign()])) as client:
            await service.sync_campaigns(db, client=client)
        async with await _http_client(
            lambda _request: httpx.Response(200, json={"success": False, "data": {"campaigns": []}})
        ) as client:
            with pytest.raises(service.VCommissionSyncError, match="unsuccessful"):
                await service.sync_campaigns(db, client=client)
        campaign = await db.scalar(select(AffiliateCampaign))
        latest = await db.scalar(
            select(AffiliateCampaignSync).order_by(AffiliateCampaignSync.started_at.desc())
        )

    assert campaign is not None and campaign.is_active
    assert latest is not None and latest.status == "failed"


async def _add_campaign(async_session_maker) -> uuid.UUID:
    campaign = AffiliateCampaign(
        network="vcommission",
        external_campaign_id="campaign-1",
        title="Summer offer",
        description="Payout 12% new users. Promotion Methods: Telegram - Not Allowed",
        kpi="Cancelled orders are not payable",
        merchant_name="Example Shop",
        tracking_url="https://track.example/offer?token=secret",
        categories=["Fashion"],
        countries=["IN"],
        payouts=[{"amount": 12}],
        network_metadata={},
        is_active=True,
        last_synced_at=datetime.now(UTC),
    )
    async with async_session_maker() as db:
        db.add(campaign)
        await db.commit()
        return campaign.id


@pytest.mark.asyncio
async def test_campaign_catalogue_is_not_public(async_session_maker, client, make_headers):
    """Payouts, advertiser terms and KPI rules are commercial data, not shopper content."""
    campaign_id = await _add_campaign(async_session_maker)

    for path in (
        "/api/v1/vcommission/campaigns",
        f"/api/v1/vcommission/campaigns/{campaign_id}",
    ):
        anonymous = await client.get(path)
        assert anonymous.status_code == 401, path
        assert "payout" not in anonymous.text.lower()

        shopper = await client.get(path, headers=await make_headers(Role.user))
        assert shopper.status_code == 403, path
        assert "payout" not in shopper.text.lower()


@pytest.mark.asyncio
async def test_admin_can_read_campaigns_and_responses_are_never_cacheable_publicly(
    async_session_maker, client, admin_headers
):
    campaign_id = await _add_campaign(async_session_maker)

    listing = await client.get(
        "/api/v1/vcommission/campaigns?category=Fashion&country=IN", headers=admin_headers
    )
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    assert "public" not in listing.headers.get("cache-control", "")

    detail = await client.get(f"/api/v1/vcommission/campaigns/{campaign_id}", headers=admin_headers)
    assert detail.status_code == 200
    assert "tracking_url" not in detail.json()
    assert "public" not in detail.headers.get("cache-control", "")


def test_campaign_routes_are_not_on_the_public_cache_allow_list():
    from app.core.cache_headers import cache_control_for

    assert cache_control_for("/api/v1/vcommission/campaigns") is None
    assert cache_control_for("/api/v1/vcommission/campaigns/abc") is None


@pytest.mark.asyncio
async def test_redirect_tracks_click_and_stays_public(async_session_maker, client):
    """The shopper-facing tracked redirect must keep working without a login."""
    campaign_id = await _add_campaign(async_session_maker)

    redirect = await client.get(
        f"/api/v1/vcommission/campaigns/{campaign_id}/go",
        headers={"user-agent": "Mozilla/5.0"},
        follow_redirects=False,
    )
    assert redirect.status_code == 302
    assert redirect.headers["location"] == "https://track.example/offer?token=secret"
    assert "cache-control" not in redirect.headers
