"""Outbound click tracking and affiliate link building.

The property under test: every redirect through /go leaves exactly one durable
record, carries that record's reference to the affiliate network, and never
stores who the visitor was beyond a one-way hash.
"""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import func, select

from app.models.click import ClickEvent
from app.models.coupon import Coupon
from app.services import affiliate, click_service

COUPONS = "/api/v1/coupons"
PRODUCTS = "/api/v1/products"
HUMAN = {"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) AppleWebKit/605.1.15 Safari/604.1"}


# ------------------------------------------------------------ pure link builder


def test_awin_appends_clickref():
    url = affiliate.build_outbound_url(
        "https://www.awin1.com/cread.php?awinmid=1&awinaffid=2",
        network="awin",
        link_template=None,
        clickref="abc123",
    )
    q = parse_qs(urlparse(url).query)
    assert q["clickref"] == ["abc123"]
    assert q["awinmid"] == ["1"] and q["awinaffid"] == ["2"]


def test_existing_reference_is_replaced_not_duplicated():
    url = affiliate.build_outbound_url(
        "https://x.com/p?clickref=old&a=1", network="awin", link_template=None, clickref="new"
    )
    assert parse_qs(urlparse(url).query)["clickref"] == ["new"]
    assert url.count("clickref=") == 1


@pytest.mark.parametrize(
    ("network", "param"),
    [("admitad", "subid"), ("impact", "subId1")],
)
def test_other_networks_use_their_own_parameter(network, param):
    url = affiliate.build_outbound_url(
        "https://shop.example/item", network=network, link_template=None, clickref="r1"
    )
    assert parse_qs(urlparse(url).query)[param] == ["r1"]


@pytest.mark.parametrize("network", ["none", "direct", None, "", "nonsense"])
def test_unwrapped_networks_leave_the_url_alone(network):
    dest = "https://shop.example/item?x=1"
    assert (
        affiliate.build_outbound_url(dest, network=network, link_template=None, clickref="r")
        == dest
    )


def test_template_fills_placeholders_and_encodes_destination():
    url = affiliate.build_outbound_url(
        "https://shop.example/item?a=1&b=2",
        network="awin",
        link_template="https://track.example/go?ref={clickref}&to={url}&raw={raw_url}",
        clickref="zz",
    )
    assert "ref=zz" in url
    assert "to=https%3A%2F%2Fshop.example%2Fitem%3Fa%3D1%26b%3D2" in url
    assert url.endswith("raw=https://shop.example/item?a=1&b=2")


def test_non_http_template_result_falls_back_to_destination():
    dest = "https://shop.example/item"
    assert (
        affiliate.build_outbound_url(
            dest, network="awin", link_template="javascript:alert({clickref})", clickref="x"
        )
        == dest
    )


def test_non_http_destination_is_never_wrapped():
    dest = "ftp://files.example/x"
    assert (
        affiliate.build_outbound_url(dest, network="awin", link_template=None, clickref="x") == dest
    )


def test_src_normalisation_accepts_only_short_boring_tags():
    assert click_service.normalize_src("Coupon-Page") == "coupon-page"
    assert click_service.normalize_src(None) == "unknown"
    assert click_service.normalize_src("<script>alert(1)</script>") == "unknown"
    assert click_service.normalize_src("a b") == "unknown"
    assert click_service.normalize_src("-leading") == "unknown"


@pytest.mark.parametrize(
    "ua", [None, "", "Googlebot/2.1", "curl/8.0", "Mozilla/5.0 (compatible; bingbot/2.0)"]
)
def test_bots_are_flagged(ua):
    assert click_service.looks_like_bot(ua) is True


def test_real_browser_is_not_flagged():
    assert click_service.looks_like_bot(HUMAN["user-agent"]) is False


# ------------------------------------------------------------------ helpers


async def _make_store(client, headers, **extra):
    resp = await client.post("/api/v1/stores", json={"name": "Shop", **extra}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _make_coupon(client, headers, store_id, dest="https://shop.example/deal"):
    cat = (await client.post("/api/v1/categories", json={"name": "Misc"}, headers=headers)).json()
    resp = await client.post(
        COUPONS,
        json={
            "title": "10% off",
            "code": "TEN",
            "discount_type": "percentage",
            "discount_value": 10,
            "store_id": store_id,
            "category_id": cat["id"],
            "destination_url": dest,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json(), cat["id"]


async def _clicks(async_session_maker):
    async with async_session_maker() as db:
        return list((await db.execute(select(ClickEvent))).scalars())


# ------------------------------------------------------------ coupon /go


async def test_go_records_click_and_sends_reference_to_awin(
    client, admin_headers, async_session_maker
):
    store = await _make_store(client, admin_headers, affiliate_network="awin")
    coupon, _ = await _make_coupon(client, admin_headers, store["id"])

    resp = await client.get(
        f"{COUPONS}/{coupon['id']}/go?src=coupon-page", headers=HUMAN, follow_redirects=False
    )
    assert resp.status_code == 302

    clicks = await _clicks(async_session_maker)
    assert len(clicks) == 1
    click = clicks[0]
    assert click.src == "coupon-page"
    assert click.network == "awin"
    assert str(click.coupon_id) == coupon["id"]
    assert str(click.store_id) == store["id"]
    assert click.is_bot is False
    assert parse_qs(urlparse(resp.headers["location"]).query)["clickref"] == [click.clickref]

    assert (await client.get(f"{COUPONS}/{coupon['id']}")).json()["clicks_count"] == 1


async def test_go_without_affiliate_network_redirects_unchanged(client, admin_headers):
    store = await _make_store(client, admin_headers)
    coupon, _ = await _make_coupon(client, admin_headers, store["id"], "https://shop.example/d?x=1")
    resp = await client.get(f"{COUPONS}/{coupon['id']}/go", headers=HUMAN, follow_redirects=False)
    assert resp.headers["location"] == "https://shop.example/d?x=1"


async def test_go_uses_store_link_template(client, admin_headers):
    store = await _make_store(
        client,
        admin_headers,
        affiliate_network="cuelinks",
        link_template="https://linksredirect.example/?subid={clickref}&dl={url}",
    )
    coupon, _ = await _make_coupon(client, admin_headers, store["id"])
    resp = await client.get(f"{COUPONS}/{coupon['id']}/go", headers=HUMAN, follow_redirects=False)
    loc = resp.headers["location"]
    assert loc.startswith("https://linksredirect.example/?subid=")
    assert "dl=https%3A%2F%2Fshop.example%2Fdeal" in loc


async def test_rapid_repeat_is_one_click_with_one_reference(
    client, admin_headers, async_session_maker
):
    store = await _make_store(client, admin_headers, affiliate_network="awin")
    coupon, _ = await _make_coupon(client, admin_headers, store["id"])

    first = await client.get(f"{COUPONS}/{coupon['id']}/go", headers=HUMAN, follow_redirects=False)
    second = await client.get(f"{COUPONS}/{coupon['id']}/go", headers=HUMAN, follow_redirects=False)

    assert first.headers["location"] == second.headers["location"]
    assert len(await _clicks(async_session_maker)) == 1
    assert (await client.get(f"{COUPONS}/{coupon['id']}")).json()["clicks_count"] == 1


async def test_bot_click_is_recorded_but_flagged(client, admin_headers, async_session_maker):
    store = await _make_store(client, admin_headers)
    coupon, _ = await _make_coupon(client, admin_headers, store["id"])
    await client.get(
        f"{COUPONS}/{coupon['id']}/go",
        headers={"user-agent": "Googlebot/2.1"},
        follow_redirects=False,
    )
    (click,) = await _clicks(async_session_maker)
    assert click.is_bot is True


async def test_hostile_src_is_stored_as_unknown(client, admin_headers, async_session_maker):
    store = await _make_store(client, admin_headers)
    coupon, _ = await _make_coupon(client, admin_headers, store["id"])
    resp = await client.get(
        f"{COUPONS}/{coupon['id']}/go",
        params={"src": "<b>x</b>"},
        headers=HUMAN,
        follow_redirects=False,
    )
    assert resp.status_code == 302
    (click,) = await _clicks(async_session_maker)
    assert click.src == "unknown"


async def test_overlong_src_is_rejected(client, admin_headers):
    store = await _make_store(client, admin_headers)
    coupon, _ = await _make_coupon(client, admin_headers, store["id"])
    resp = await client.get(
        f"{COUPONS}/{coupon['id']}/go", params={"src": "a" * 41}, follow_redirects=False
    )
    assert resp.status_code == 422


async def test_stores_hash_not_raw_ip(client, admin_headers, async_session_maker):
    store = await _make_store(client, admin_headers)
    coupon, _ = await _make_coupon(client, admin_headers, store["id"])
    await client.get(
        f"{COUPONS}/{coupon['id']}/go",
        headers={**HUMAN, "x-forwarded-for": "203.0.113.9"},
        follow_redirects=False,
    )
    (click,) = await _clicks(async_session_maker)
    assert click.visitor_hash is not None
    assert len(click.visitor_hash) == 64
    assert "203.0.113.9" not in click.visitor_hash


async def test_inactive_coupon_records_nothing(client, admin_headers, async_session_maker):
    store = await _make_store(client, admin_headers)
    coupon, _ = await _make_coupon(client, admin_headers, store["id"])
    await client.patch(
        f"{COUPONS}/{coupon['id']}", json={"is_active": False}, headers=admin_headers
    )
    resp = await client.get(f"{COUPONS}/{coupon['id']}/go", headers=HUMAN, follow_redirects=False)
    assert resp.status_code == 404
    assert await _clicks(async_session_maker) == []


async def test_deleting_a_coupon_keeps_its_click_history(
    client, admin_headers, async_session_maker
):
    store = await _make_store(client, admin_headers)
    coupon, _ = await _make_coupon(client, admin_headers, store["id"])
    await client.get(f"{COUPONS}/{coupon['id']}/go", headers=HUMAN, follow_redirects=False)
    async with async_session_maker() as db:
        obj = await db.get(Coupon, __import__("uuid").UUID(coupon["id"]))
        await db.delete(obj)
        await db.commit()
    (click,) = await _clicks(async_session_maker)
    assert click.coupon_id is None  # SET NULL: the click (and any revenue) survives


# ------------------------------------------------------------- store config


async def test_store_rejects_unknown_network(client, admin_headers):
    resp = await client.post(
        "/api/v1/stores",
        json={"name": "Bad", "affiliate_network": "shadynet"},
        headers=admin_headers,
    )
    assert resp.status_code == 422


async def test_store_rejects_non_http_template(client, admin_headers):
    resp = await client.post(
        "/api/v1/stores",
        json={"name": "Bad", "link_template": "javascript:alert(1)"},
        headers=admin_headers,
    )
    assert resp.status_code == 422


async def test_public_store_json_does_not_expose_affiliate_wiring(client, admin_headers):
    store = await _make_store(
        client,
        admin_headers,
        affiliate_network="awin",
        link_template="https://t.example/?r={clickref}&u={url}",
        cookie_days=30,
    )
    body = (await client.get(f"/api/v1/stores/{store['id']}")).json()
    for leaked in ("affiliate_network", "link_template", "cookie_days"):
        assert leaked not in body


# -------------------------------------------------------------- product /go


async def _make_product(client, headers, store_id, cat_id, **extra):
    resp = await client.post(
        PRODUCTS,
        json={"name": "Kurta", "store_id": store_id, "category_id": cat_id, **extra},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_product_json_hides_url_but_reports_whether_one_exists(client, admin_headers):
    store = await _make_store(client, admin_headers)
    _, cat_id = await _make_coupon(client, admin_headers, store["id"])
    with_url = await _make_product(
        client, admin_headers, store["id"], cat_id, url="https://shop.example/kurta"
    )
    without = await _make_product(client, admin_headers, store["id"], cat_id, name="Saree")

    got = (await client.get(f"{PRODUCTS}/{with_url['id']}")).json()
    assert "url" not in got
    assert got["has_url"] is True
    assert (await client.get(f"{PRODUCTS}/{without['id']}")).json()["has_url"] is False


async def test_product_go_is_tracked_and_wrapped(client, admin_headers, async_session_maker):
    store = await _make_store(client, admin_headers, affiliate_network="awin")
    _, cat_id = await _make_coupon(client, admin_headers, store["id"])
    product = await _make_product(
        client, admin_headers, store["id"], cat_id, url="https://shop.example/kurta"
    )

    resp = await client.get(
        f"{PRODUCTS}/{product['id']}/go?src=product-page", headers=HUMAN, follow_redirects=False
    )
    assert resp.status_code == 302
    (click,) = await _clicks(async_session_maker)
    assert str(click.product_id) == product["id"]
    assert click.coupon_id is None
    assert click.src == "product-page"
    assert parse_qs(urlparse(resp.headers["location"]).query)["clickref"] == [click.clickref]


async def test_product_go_404s_without_a_url(client, admin_headers):
    store = await _make_store(client, admin_headers)
    _, cat_id = await _make_coupon(client, admin_headers, store["id"])
    product = await _make_product(client, admin_headers, store["id"], cat_id)
    resp = await client.get(f"{PRODUCTS}/{product['id']}/go", follow_redirects=False)
    assert resp.status_code == 404


# ----------------------------------------------------------- privacy retention


async def test_scrub_forgets_visitors_but_keeps_the_click(async_session_maker):
    old = datetime.now(UTC) - timedelta(days=120)
    async with async_session_maker() as db:
        db.add_all(
            [
                ClickEvent(
                    clickref="old" + "0" * 29,
                    src="x",
                    network="awin",
                    visitor_hash="h" * 64,
                    is_bot=False,
                    created_at=old,
                ),
                ClickEvent(
                    clickref="new" + "0" * 29,
                    src="x",
                    network="awin",
                    visitor_hash="h" * 64,
                    is_bot=False,
                ),
            ]
        )
        await db.commit()

    async with async_session_maker() as db:
        scrubbed = await click_service.scrub_visitor_hashes(db, older_than_days=90)
    assert scrubbed == 1

    async with async_session_maker() as db:
        rows = {c.clickref[:3]: c for c in (await db.execute(select(ClickEvent))).scalars()}
        assert await db.scalar(select(func.count()).select_from(ClickEvent)) == 2
    assert rows["old"].visitor_hash is None
    assert rows["new"].visitor_hash is not None


async def test_store_update_can_set_and_reset_network(client, admin_headers):
    store = await _make_store(client, admin_headers)
    ok = await client.patch(
        f"/api/v1/stores/{store['id']}",
        json={"affiliate_network": "admitad", "cookie_days": 45},
        headers=admin_headers,
    )
    assert ok.status_code == 200, ok.text
    reset = await client.patch(
        f"/api/v1/stores/{store['id']}", json={"affiliate_network": None}, headers=admin_headers
    )
    assert reset.status_code == 200, reset.text
