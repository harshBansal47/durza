"""Phase 5 — AffiliateRouter: choosing which provider's link a click goes to.

The contract under test, in one line: **precedence decides, and nothing else
does**. Every test here exists to pin one part of that, or to pin one of the ways
it must *not* decide — a broken ingest API, a source an operator switched off, a
source for the wrong market, or a fabricated performance metric.
"""

import uuid
from datetime import UTC, datetime

import pytest

from app.models.category import Category
from app.models.coupon import Coupon, DiscountType
from app.models.coupon_source import CouponSource, CouponSourceStatus
from app.models.source import Source, SourceKind, SourceStatus
from app.models.store import Store
from app.services.affiliate_router import (
    ELIGIBLE_SOURCE_STATUSES,
    RoutingDecision,
    eligible_sources,
    route_coupon,
)

pytestmark = pytest.mark.asyncio


# ------------------------------------------------------------------ helpers


async def _store(async_session_maker, **overrides) -> Store:
    name = overrides.pop("name", "Nike")
    async with async_session_maker() as db:
        store = Store(
            name=name,
            slug=f"{name.lower()}-{uuid.uuid4().hex[:6]}",
            normalized_name=name.lower(),
            normalized_domain=f"{name.lower()}.com",
            **overrides,
        )
        db.add(store)
        await db.commit()
        await db.refresh(store)
        return store


async def _category(async_session_maker, name="Shoes") -> Category:
    async with async_session_maker() as db:
        cat = Category(name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:6]}")
        db.add(cat)
        await db.commit()
        await db.refresh(cat)
        return cat


async def _coupon(async_session_maker, store, category, **overrides) -> Coupon:
    payload = {
        "title": "20% off all trainers",
        "slug": f"off-trainers-{uuid.uuid4().hex[:6]}",
        "code": "SAVE20",
        "discount_type": DiscountType.percentage,
        "discount_value": 20,
        "store_id": store.id,
        "category_id": category.id,
        "destination_url": "https://www.nike.com/offers",
    }
    payload.update(overrides)
    async with async_session_maker() as db:
        coupon = Coupon(**payload)
        db.add(coupon)
        await db.commit()
        await db.refresh(coupon)
        return coupon


async def _source(async_session_maker, name="Awin US", **overrides) -> Source:
    payload = {
        "name": f"{name} {uuid.uuid4().hex[:4]}",
        "slug": f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}",
        "kind": SourceKind.awin,
        "config": {},
    }
    payload.update(overrides)
    async with async_session_maker() as db:
        source = Source(**payload)
        db.add(source)
        await db.commit()
        await db.refresh(source)
        return source


async def _claim(async_session_maker, coupon, source, url: str, **overrides) -> None:
    """Give one source its own record of this coupon."""
    payload = {
        "coupon_id": coupon.id,
        "source_id": source.id,
        "affiliate_url": url,
        "status": CouponSourceStatus.active,
        "external_id": f"ext-{uuid.uuid4().hex[:6]}",
        "last_seen_at": datetime.now(UTC),
    }
    payload.update(overrides)
    async with async_session_maker() as db:
        db.add(CouponSource(**payload))
        await db.commit()


async def _route(async_session_maker, coupon):
    async with async_session_maker() as db:
        return await route_coupon(db, coupon)


@pytest.fixture
async def world(async_session_maker):
    return {
        "store": await _store(async_session_maker),
        "category": await _category(async_session_maker),
    }


# ------------------------------------------------------------------ ordering


async def test_the_highest_precedence_source_wins(async_session_maker, world):
    coupon = await _coupon(async_session_maker, world["store"], world["category"])
    low = await _source(async_session_maker, "cj feed", priority=80)
    high = await _source(async_session_maker, "rakuten", priority=10)
    await _claim(async_session_maker, coupon, low, "https://cj.example/deep")
    await _claim(async_session_maker, coupon, high, "https://rakuten.example/deep")

    routed = await _route(async_session_maker, coupon)

    assert routed.url == "https://rakuten.example/deep"
    assert routed.source_slug == high.slug


async def test_routed_admitad_source_carries_its_tracking_network(async_session_maker, world):
    coupon = await _coupon(async_session_maker, world["store"], world["category"])
    admitad = await _source(async_session_maker, "admitad", kind=SourceKind.admitad)
    await _claim(async_session_maker, coupon, admitad, "https://admitad.example/click")

    routed = await _route(async_session_maker, coupon)

    assert routed.network == "admitad"
    assert routed.decision is RoutingDecision.source
    assert routed.candidates == 2


async def test_priority_beats_a_more_confident_source(async_session_maker, world):
    """Priority is the operator's decision and outranks the derived confidence.

    Otherwise the two axes are not comparable and an operator re-ordering who
    wins would be editing a number that has no effect whenever the other axis
    moves — which is how "we trust this advertiser more" quietly stops meaning
    anything.
    """
    coupon = await _coupon(async_session_maker, world["store"], world["category"])
    certain_but_low = await _source(async_session_maker, "confident", priority=50, confidence=0.99)
    unsure_but_high = await _source(async_session_maker, "trusted", priority=1, confidence=0.2)
    await _claim(async_session_maker, coupon, certain_but_low, "https://low.example/deep")
    await _claim(async_session_maker, coupon, unsure_but_high, "https://high.example/deep")

    assert (await _route(async_session_maker, coupon)).url == "https://high.example/deep"


async def test_confidence_breaks_a_priority_tie(async_session_maker, world):
    coupon = await _coupon(async_session_maker, world["store"], world["category"])
    timid = await _source(async_session_maker, "timid", priority=100, confidence=0.3)
    assured = await _source(async_session_maker, "assured", priority=100, confidence=0.9)
    await _claim(async_session_maker, coupon, timid, "https://timid.example/deep")
    await _claim(async_session_maker, coupon, assured, "https://assured.example/deep")

    assert (await _route(async_session_maker, coupon)).url == "https://assured.example/deep"


async def test_ordering_is_total_so_the_winner_is_reproducible(async_session_maker, world):
    """Two sources identical in every tuned field still resolve the same way twice.

    Every source defaults to priority 100 / confidence 0.5, so this is the
    common case, not an edge case. Without a final tiebreak the winner would be
    whatever order the database returned, and a redirect that varies run to run
    is not reproducible when a merchant asks why their traffic split changed.
    """
    coupon = await _coupon(async_session_maker, world["store"], world["category"])
    a = await _source(async_session_maker, "alpha", slug=f"a-{uuid.uuid4().hex[:6]}")
    b = await _source(async_session_maker, "beta", slug=f"b-{uuid.uuid4().hex[:6]}")
    await _claim(async_session_maker, coupon, a, "https://a.example/deep")
    await _claim(async_session_maker, coupon, b, "https://b.example/deep")

    first = await _route(async_session_maker, coupon)
    second = await _route(async_session_maker, coupon)

    assert first.source_slug == second.source_slug
    # Slug decides, and "a-" sorts before "b-".
    assert first.source_slug == a.slug


# ------------------------------------------------------- status of the claim


async def test_a_live_source_outranks_an_expired_one(async_session_maker, world):
    """An expired deep link is still a real URL, but the live one is the better send.

    The coupon is on the site, so at least one provider still sells it. Sending
    the shopper to a higher-priority provider that has already called the code
    dead would point them at a page the merchant has retired.
    """
    coupon = await _coupon(async_session_maker, world["store"], world["category"])
    expired_but_trusted = await _source(async_session_maker, "trusted", priority=1)
    live_but_ordinary = await _source(async_session_maker, "ordinary", priority=90)
    await _claim(
        async_session_maker,
        coupon,
        expired_but_trusted,
        "https://expired.example/deep",
        status=CouponSourceStatus.expired,
    )
    await _claim(async_session_maker, coupon, live_but_ordinary, "https://live.example/deep")

    routed = await _route(async_session_maker, coupon)

    assert routed.url == "https://live.example/deep"
    assert routed.source_slug == live_but_ordinary.slug
    assert routed.candidates == 2


async def test_an_expired_source_is_still_used_when_it_is_all_there_is(async_session_maker, world):
    """Preferring live claims must not mean refusing to route.

    A coupon whose only source has expired it is still on the site, and the
    merchant's page is still the best URL we hold. Falling through to the
    denormalized destination would send the shopper to the same place anyway, but
    through a code path that cannot say which provider it came from.
    """
    coupon = await _coupon(async_session_maker, world["store"], world["category"])
    only = await _source(async_session_maker, "only")
    await _claim(
        async_session_maker,
        coupon,
        only,
        "https://expired.example/deep",
        status=CouponSourceStatus.expired,
    )

    routed = await _route(async_session_maker, coupon)

    assert routed.url == "https://expired.example/deep"
    assert routed.source_status is CouponSourceStatus.expired


@pytest.mark.parametrize(
    "status",
    [CouponSourceStatus.deleted, CouponSourceStatus.inactive, CouponSourceStatus.failed],
)
async def test_a_source_that_no_longer_claims_the_coupon_is_never_used(
    async_session_maker, world, status
):
    """These statuses say the offer is gone or unreadable, not merely aged out."""
    coupon = await _coupon(async_session_maker, world["store"], world["category"])
    trusted = await _source(async_session_maker, "trusted", priority=1)
    ordinary = await _source(async_session_maker, "ordinary", priority=90)
    await _claim(async_session_maker, coupon, trusted, "https://gone.example/deep", status=status)
    await _claim(async_session_maker, coupon, ordinary, "https://ordinary.example/deep")

    routed = await _route(async_session_maker, coupon)

    assert routed.url == "https://ordinary.example/deep"


async def test_the_eligible_status_set_is_the_two_documented_ones():
    """Pin the set itself, so adding a status is a deliberate change to the rules."""
    assert set(ELIGIBLE_SOURCE_STATUSES) == {CouponSourceStatus.active, CouponSourceStatus.expired}


# ------------------------------------------------------------ operator intent


async def test_a_disabled_source_is_never_used(async_session_maker, world):
    """`is_enabled` is the one signal strong enough to retire a link."""
    coupon = await _coupon(async_session_maker, world["store"], world["category"])
    off = await _source(async_session_maker, "switched off", priority=1, is_enabled=False)
    on = await _source(async_session_maker, "on", priority=90)
    await _claim(async_session_maker, coupon, off, "https://off.example/deep")
    await _claim(async_session_maker, coupon, on, "https://on.example/deep")

    assert (await _route(async_session_maker, coupon)).url == "https://on.example/deep"


@pytest.mark.parametrize(
    "api_status", [SourceStatus.degraded, SourceStatus.failing, SourceStatus.unconfigured]
)
async def test_a_broken_ingest_api_does_not_take_a_link_offline(
    async_session_maker, world, api_status
):
    """`api_status` records whether *we* can read a catalogue, not whether the
    merchant's deep link works.

    A CJ key expiring must not disable CJ's links for every coupon CJ ever
    contributed — the merchant's server never noticed, and those links are still
    the ones the shopper should be sent to.
    """
    coupon = await _coupon(async_session_maker, world["store"], world["category"])
    broken = await _source(async_session_maker, "cj", api_status=api_status, priority=1)
    await _claim(async_session_maker, coupon, broken, "https://cj.example/deep")

    routed = await _route(async_session_maker, coupon)

    assert routed.url == "https://cj.example/deep"


# -------------------------------------------------------------- market scope


async def test_a_source_outside_the_coupons_market_is_skipped(async_session_maker, world):
    coupon = await _coupon(async_session_maker, world["store"], world["category"], country="US")
    wrong_market = await _source(async_session_maker, "german", priority=1, countries=["DE"])
    right_market = await _source(async_session_maker, "american", priority=90, countries=["US"])
    await _claim(async_session_maker, coupon, wrong_market, "https://de.example/deep")
    await _claim(async_session_maker, coupon, right_market, "https://us.example/deep")

    assert (await _route(async_session_maker, coupon)).url == "https://us.example/deep"


async def test_a_source_with_no_markets_is_eligible_everywhere(async_session_maker, world):
    """Empty `countries` means "no opinion", not "nowhere".

    A source with no market restriction is far more likely to be a global feed
    than a mislabelled one; excluding it would discard good links on a technicality
    no operator ever set.
    """
    coupon = await _coupon(async_session_maker, world["store"], world["category"], country="US")
    unscoped = await _source(async_session_maker, "global", priority=90, countries=[])
    await _claim(async_session_maker, coupon, unscoped, "https://global.example/deep")

    assert (await _route(async_session_maker, coupon)).url == "https://global.example/deep"


async def test_no_source_in_the_market_falls_back_rather_than_routing_wrong(
    async_session_maker, world
):
    coupon = await _coupon(async_session_maker, world["store"], world["category"], country="US")
    only_de = await _source(async_session_maker, "german", priority=1, countries=["DE"])
    await _claim(async_session_maker, coupon, only_de, "https://de.example/deep")

    routed = await _route(async_session_maker, coupon)

    assert routed.decision is RoutingDecision.fallback
    assert routed.url == coupon.destination_url


async def test_a_coupon_with_no_market_accepts_any_source(async_session_maker, world):
    coupon = await _coupon(async_session_maker, world["store"], world["category"])
    scoped = await _source(async_session_maker, "german", countries=["DE"])
    await _claim(async_session_maker, coupon, scoped, "https://de.example/deep")

    assert (await _route(async_session_maker, coupon)).url == "https://de.example/deep"


# --------------------------------------------------------------- the fallback


async def test_a_coupon_with_no_sources_falls_back_to_its_destination(async_session_maker, world):
    """Hand-created coupons have no source rows and still have to redirect."""
    coupon = await _coupon(async_session_maker, world["store"], world["category"])

    routed = await _route(async_session_maker, coupon)

    assert routed.decision is RoutingDecision.fallback
    assert routed.url == coupon.destination_url
    assert routed.source_slug is None
    assert routed.candidates == 0


async def test_a_source_row_without_a_link_is_not_a_candidate(async_session_maker, world):
    """An offer can be real and still have no affiliate URL. It cannot be a target."""
    coupon = await _coupon(async_session_maker, world["store"], world["category"])
    sourceless = await _source(async_session_maker, "no link", priority=1)
    with_link = await _source(async_session_maker, "with link", priority=90)
    await _claim(async_session_maker, coupon, sourceless, None)
    await _claim(async_session_maker, coupon, with_link, "https://with.example/deep")

    routed = await _route(async_session_maker, coupon)

    assert routed.url == "https://with.example/deep"
    assert routed.candidates == 1


async def test_only_the_winning_source_is_reported(async_session_maker, world):
    """One click, one destination — and one named source behind it.

    `RoutedDestination.source_slug` is for logs and the admin view. Asserting the
    losers are absent is what keeps a future change from turning this into a
    list, which some caller would eventually serialise.
    """
    coupon = await _coupon(async_session_maker, world["store"], world["category"])
    winner = await _source(async_session_maker, "winner", priority=1)
    loser = await _source(async_session_maker, "loser", priority=90)
    await _claim(async_session_maker, coupon, winner, "https://winner.example/deep")
    await _claim(async_session_maker, coupon, loser, "https://loser.example/deep")

    routed = await _route(async_session_maker, coupon)

    assert routed.source_slug == winner.slug
    assert loser.slug not in {routed.source_slug}


# ------------------------------------------------------------- what is gone


async def test_no_fabricated_scoring_survives_in_the_router():
    """The old model ranked providers on metrics we invented.

    Estimated clicks as `conversions * 20` made EPC algebraically identical to
    commission — the same number under two names — so a minimum-sample threshold
    could not catch it, because the fabricated click count scaled with the very
    number it was meant to qualify. None of it may quietly return as a
    tiebreaker, so the guard is structural: no such name may exist on the module
    and no such field on the result. That survives rewording the docstrings,
    which a source-text search would not.
    """
    import app.services.affiliate_router as router

    exported = [name.lower() for name in dir(router)]
    for invented in ("epc", "commission", "weight", "score", "reliab", "sample_size"):
        assert not [name for name in exported if invented in name], (
            f"{invented!r} came back into the router"
        )

    result_fields = set(router.RoutedDestination.__dataclass_fields__)
    assert not {"score", "breakdown", "weights"} & result_fields


# -------------------------------------------------------------- the /go route


async def test_the_redirect_uses_the_routed_link_and_never_leaks_the_provider(
    client, admin_headers, async_session_maker
):
    """End to end: the shopper lands on the winning source's URL, and the
    response says nothing at all about which provider that was."""
    store = (
        await client.post("/api/v1/stores", json={"name": "Nike"}, headers=admin_headers)
    ).json()
    cat = (
        await client.post("/api/v1/categories", json={"name": "Shoes"}, headers=admin_headers)
    ).json()
    created = (
        await client.post(
            "/api/v1/coupons",
            json={
                "title": "20% off trainers",
                "code": "SAVE20",
                "discount_type": "percentage",
                "discount_value": 20,
                "store_id": store["id"],
                "category_id": cat["id"],
                "destination_url": "https://www.nike.com/offers",
            },
            headers=admin_headers,
        )
    ).json()

    # Attach a losing and a winning source to the coupon the API actually created.
    async with async_session_maker() as db:
        real = await db.get(Coupon, uuid.UUID(created["id"]))
    trusted = await _source(async_session_maker, "trusted", priority=1)
    ordinary = await _source(async_session_maker, "ordinary", priority=90)
    await _claim(async_session_maker, real, ordinary, "https://ordinary.example/deep")
    await _claim(async_session_maker, real, trusted, "https://trusted.example/deep")

    resp = await client.get(f"/api/v1/coupons/{created['id']}/go", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"] == "https://trusted.example/deep"
    # The provider's slug is internal: not in the header, not in a body.
    assert trusted.slug not in resp.text
    assert resp.content == b""


async def test_a_coupon_with_nowhere_to_go_is_404_not_a_redirect_loop(
    client, admin_headers, async_session_maker, world
):
    """`Location: ""` resolves against the current path and bounces forever.

    `CouponCreate.destination_url` is `min_length=1`, so the API cannot produce
    this — but a seed script, a migration or a hand-written row can, and the
    column is `nullable=False` rather than non-empty. The redirect is the last
    line before a shopper is sent somewhere, so it refuses rather than guesses.
    """
    async with async_session_maker() as db:
        broken = await _coupon(
            async_session_maker,
            world["store"],
            world["category"],
            slug=f"broken-{uuid.uuid4().hex[:6]}",
            destination_url="",
        )
        await db.commit()

    resp = await client.get(f"/api/v1/coupons/{broken.id}/go", follow_redirects=False)

    assert resp.status_code == 404
    assert "location" not in resp.headers


# ------------------------------------------------------------------ internals


async def test_eligible_sources_returns_rows_most_trusted_first(async_session_maker, world):
    """The public helper and the routing decision must not disagree about order."""
    coupon = await _coupon(async_session_maker, world["store"], world["category"])
    second = await _source(
        async_session_maker, "second", slug=f"s-{uuid.uuid4().hex[:6]}", priority=50
    )
    first = await _source(
        async_session_maker, "first", slug=f"f-{uuid.uuid4().hex[:6]}", priority=10
    )
    await _claim(async_session_maker, coupon, second, "https://second.example/deep")
    await _claim(async_session_maker, coupon, first, "https://first.example/deep")

    async with async_session_maker() as db:
        rows = await eligible_sources(db, coupon)

    assert [source.slug for _, source in rows] == [first.slug, second.slug]
