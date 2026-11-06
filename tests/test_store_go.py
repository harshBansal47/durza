"""`/stores/by-slug/{slug}/go`: where a "Go to store" click is sent, and what it records.

Rules under test (see app/services/store_router.py): the store's own configured link wins;
otherwise an advertiser campaign matched to the store, but only if that advertiser's own rules
allow the channel the click came from; otherwise the plain website. Compliance can only remove
an option. The network is never revealed to the browser.
"""

import uuid
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from app.models.affiliate_campaign import AffiliateCampaign
from app.models.click import ClickEvent
from app.models.store import Store
from app.services.store_router import channel_for_src

API = "/api/v1"
CAMPAIGN_URL = "https://track.example/offer?campaign_id=7&pub_id=9"
WEBSITE = "https://www.shop.example"


def allowed(*channels: str, conditional: dict[str, str] | None = None) -> dict:
    """The persisted `terms` shape: every listed channel allowed, the rest not mentioned."""
    entries = {c: {"status": "allowed", "note": None} for c in channels}
    for channel, note in (conditional or {}).items():
        entries[channel] = {"status": "conditional", "note": note}
    return {"channels": entries, "review_reasons": [], "needs_review": False}


def denied(channel: str, *also_allowed: str) -> dict:
    terms = allowed(*also_allowed)
    terms["channels"][channel] = {"status": "not_allowed", "note": None}
    return terms


async def make_store(maker, slug="shop", **fields) -> Store:
    defaults = {
        "name": slug.title(),
        "slug": slug,
        "normalized_name": slug,
        "normalized_domain": f"{slug}.example",
        "website_url": WEBSITE,
        "is_active": True,
    }
    async with maker() as db:
        store = Store(**{**defaults, **fields})
        db.add(store)
        await db.commit()
        await db.refresh(store)
        return store


async def make_campaign(maker, store: Store | None, *, terms, **fields) -> AffiliateCampaign:
    defaults = {
        "network": "vcommission",
        "external_campaign_id": f"c-{uuid.uuid4().hex[:6]}",
        "title": "Campaign",
        "merchant_name": "Shop",
        "tracking_url": CAMPAIGN_URL,
        "categories": [],
        "countries": [],
        "payouts": [],
        "network_metadata": {},
        "is_active": True,
        "last_synced_at": datetime.now(UTC),
        "store_id": store.id if store else None,
        "terms": terms,
        "needs_review": False,
    }
    async with maker() as db:
        campaign = AffiliateCampaign(**{**defaults, **fields})
        db.add(campaign)
        await db.commit()
        await db.refresh(campaign)
        return campaign


async def go(client, slug="shop", **params):
    return await client.get(
        f"{API}/stores/by-slug/{slug}/go",
        params=params,
        headers={"user-agent": "Mozilla/5.0 (test)"},
        follow_redirects=False,
    )


async def clicks(maker) -> list[ClickEvent]:
    async with maker() as db:
        return list(
            (await db.execute(select(ClickEvent).order_by(ClickEvent.created_at))).scalars()
        )


# ------------------------------------------------------------------ channel mapping


@pytest.mark.parametrize(
    ("src", "channel"),
    [
        ("telegram", "telegram"),
        ("Telegram", "telegram"),
        ("email", "email"),
        ("newsletter", "email"),
        ("sms", "sms"),
        ("store-page", "content"),
        ("homepage", "content"),
        ("category", "content"),
        ("site", "content"),
        (None, "content"),
        ("", "content"),
        ("anything-else", "content"),
    ],
)
def test_src_maps_to_the_advertiser_channel_it_counts_as(src, channel):
    assert channel_for_src(src) == channel


# ------------------------------------------------------------------ precedence


@pytest.mark.asyncio
async def test_a_matched_campaign_is_used_when_the_store_has_no_link_of_its_own(
    async_session_maker, client
):
    store = await make_store(async_session_maker)
    await make_campaign(async_session_maker, store, terms=allowed("content"))

    response = await go(client, src="store-page")

    assert response.status_code == 302
    assert response.headers["location"] == CAMPAIGN_URL
    [click] = await clicks(async_session_maker)
    assert (click.store_id, click.src, click.network) == (store.id, "store-page", "vcommission")


@pytest.mark.asyncio
async def test_the_stores_own_configured_link_always_wins(async_session_maker, client):
    store = await make_store(
        async_session_maker,
        affiliate_network="admitad",
        link_template="https://ad.example/g/abc/?ulp={url}&subid={clickref}",
    )
    await make_campaign(async_session_maker, store, terms=allowed("content"))

    response = await go(client, src="store-page")
    location = urlparse(response.headers["location"])
    query = parse_qs(location.query)

    assert location.netloc == "ad.example"
    assert query["ulp"] == [WEBSITE]
    [click] = await clicks(async_session_maker)
    assert query["subid"] == [click.clickref]  # the click is traceable to its sale
    assert click.network == "admitad"


@pytest.mark.asyncio
async def test_a_store_with_nothing_tracked_goes_to_its_website(async_session_maker, client):
    await make_store(async_session_maker)

    response = await go(client, src="homepage")

    assert response.status_code == 302
    assert response.headers["location"] == WEBSITE
    [click] = await clicks(async_session_maker)
    assert click.network == "none"  # recorded, but earns nothing


@pytest.mark.asyncio
async def test_a_store_with_no_link_at_all_is_a_404(async_session_maker, client):
    await make_store(async_session_maker, website_url=None)
    assert (await go(client)).status_code == 404
    assert await clicks(async_session_maker) == []


@pytest.mark.parametrize("slug_state", ["missing", "inactive"])
@pytest.mark.asyncio
async def test_unknown_and_inactive_stores_are_a_404(async_session_maker, client, slug_state):
    if slug_state == "inactive":
        await make_store(async_session_maker, is_active=False)
    assert (await go(client)).status_code == 404


# ------------------------------------------------------------------ compliance


@pytest.mark.asyncio
async def test_a_campaign_whose_rules_forbid_the_channel_is_not_used(async_session_maker, client):
    store = await make_store(async_session_maker)
    await make_campaign(async_session_maker, store, terms=denied("telegram", "content"))

    telegram = await go(client, src="telegram")
    site = await go(client, src="store-page")

    # Telegram is banned for this advertiser: the shopper still gets to the store, untracked.
    assert telegram.headers["location"] == WEBSITE
    # The same advertiser allows its website, so a site click is tracked.
    assert site.headers["location"] == CAMPAIGN_URL


@pytest.mark.asyncio
async def test_a_campaign_with_unreadable_terms_fails_closed(async_session_maker, client):
    store = await make_store(async_session_maker)
    await make_campaign(async_session_maker, store, terms=None)  # imported before terms existed

    assert (await go(client, src="store-page")).headers["location"] == WEBSITE


@pytest.mark.asyncio
async def test_a_conditional_channel_is_not_assumed_acceptable(async_session_maker, client):
    store = await make_store(async_session_maker)
    await make_campaign(
        async_session_maker, store, terms=allowed(conditional={"content": "Only generic"})
    )

    assert (await go(client, src="store-page")).headers["location"] == WEBSITE


@pytest.mark.asyncio
async def test_next_allowed_campaign_is_used_when_the_first_forbids_the_channel(
    async_session_maker, client
):
    store = await make_store(async_session_maker)
    await make_campaign(
        async_session_maker,
        store,
        title="A",
        terms=denied("content"),
        tracking_url="https://a.example/x",
    )
    await make_campaign(
        async_session_maker,
        store,
        title="B",
        terms=allowed("content"),
        tracking_url="https://b.example/x",
    )

    assert (await go(client, src="store-page")).headers["location"] == "https://b.example/x"


@pytest.mark.asyncio
async def test_campaigns_that_need_no_review_come_first(async_session_maker, client):
    store = await make_store(async_session_maker)
    await make_campaign(
        async_session_maker,
        store,
        title="A risky",
        terms=allowed("content"),
        needs_review=True,
        tracking_url="https://risky.example/x",
    )
    await make_campaign(
        async_session_maker,
        store,
        title="Z clean",
        terms=allowed("content"),
        tracking_url="https://clean.example/x",
    )

    assert (await go(client, src="store-page")).headers["location"] == "https://clean.example/x"


# ------------------------------------------------------------------ eligibility


@pytest.mark.asyncio
async def test_inactive_or_other_stores_campaigns_are_ignored(async_session_maker, client):
    store = await make_store(async_session_maker)
    other = await make_store(async_session_maker, slug="other")
    await make_campaign(async_session_maker, store, terms=allowed("content"), is_active=False)
    await make_campaign(async_session_maker, other, terms=allowed("content"))
    await make_campaign(async_session_maker, None, terms=allowed("content"))

    assert (await go(client, src="store-page")).headers["location"] == WEBSITE


@pytest.mark.asyncio
async def test_a_campaign_for_another_market_is_skipped(async_session_maker, client):
    store = await make_store(async_session_maker, country_code="IN")
    await make_campaign(async_session_maker, store, terms=allowed("content"), countries=["US"])

    assert (await go(client, src="store-page")).headers["location"] == WEBSITE


@pytest.mark.asyncio
async def test_a_campaign_with_no_market_restriction_is_eligible_everywhere(
    async_session_maker, client
):
    store = await make_store(async_session_maker, country_code="IN")
    await make_campaign(async_session_maker, store, terms=allowed("content"), countries=[])

    assert (await go(client, src="store-page")).headers["location"] == CAMPAIGN_URL


@pytest.mark.asyncio
async def test_a_non_http_tracking_url_is_never_redirected_to(async_session_maker, client):
    store = await make_store(async_session_maker)
    await make_campaign(
        async_session_maker, store, terms=allowed("content"), tracking_url="javascript:alert(1)"
    )

    assert (await go(client, src="store-page")).headers["location"] == WEBSITE


# ------------------------------------------------------------------ the response itself


@pytest.mark.asyncio
async def test_the_redirect_is_never_cached_or_indexed_and_reveals_no_network(
    async_session_maker, client
):
    store = await make_store(async_session_maker)
    await make_campaign(async_session_maker, store, terms=allowed("content"))

    response = await go(client, src="store-page")

    assert response.headers["x-robots-tag"] == "noindex"
    assert "cache-control" not in response.headers
    everything = (str(response.headers) + response.text).lower()
    assert "vcommission" not in everything and "admitad" not in everything


# ------------------------------------------------------------------ click recording


@pytest.mark.asyncio
async def test_two_different_stores_do_not_share_one_click(async_session_maker, client):
    """Regression: a visitor's click on store B was treated as a duplicate of their click on A."""
    a = await make_store(async_session_maker, slug="alpha")
    b = await make_store(async_session_maker, slug="beta")

    await go(client, slug="alpha", src="store-page")
    await go(client, slug="beta", src="store-page")

    recorded = await clicks(async_session_maker)
    assert sorted(c.store_id for c in recorded) == sorted([a.id, b.id])
    assert len({c.clickref for c in recorded}) == 2


@pytest.mark.asyncio
async def test_a_repeat_click_on_the_same_store_is_still_deduplicated(async_session_maker, client):
    await make_store(async_session_maker)

    await go(client, src="store-page")
    await go(client, src="store-page")

    assert len(await clicks(async_session_maker)) == 1


@pytest.mark.asyncio
async def test_src_is_normalised_and_cannot_smuggle_markup(async_session_maker, client):
    await make_store(async_session_maker)

    await go(client, src="<script>alert(1)</script>")

    [click] = await clicks(async_session_maker)
    assert click.src == "unknown"
