"""Search must treat input as literal text, and cache headers must stay on their allow-list."""

import uuid

import pytest

from app.core.cache_headers import CATALOGUE, OFFERS, cache_control_for
from app.core.search import like_contains

API = "/api/v1"


async def _setup(client, headers, store_name="Shop", category_name="Misc"):
    store = (await client.post(f"{API}/stores", json={"name": store_name}, headers=headers)).json()
    cat = (
        await client.post(f"{API}/categories", json={"name": category_name}, headers=headers)
    ).json()
    return store["id"], cat["id"]


async def _coupon(client, headers, store_id, cat_id, title, **extra):
    resp = await client.post(
        f"{API}/coupons",
        json={
            "title": title,
            "code": extra.pop("code") if "code" in extra else title.upper().replace(" ", "")[:12],
            "discount_type": "percentage",
            "discount_value": 10,
            "store_id": store_id,
            "category_id": cat_id,
            "destination_url": "https://shop.example/x",
            **extra,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _search(client, term):
    resp = await client.get(f"{API}/coupons", params={"search": term})
    assert resp.status_code == 200, resp.text
    return {c["title"] for c in resp.json()["items"]}


# ------------------------------------------------------------ escaping


def test_like_contains_escapes_wildcards_and_backslash():
    assert like_contains("plain") == "%plain%"
    assert like_contains("50%") == "%50\\%%"
    assert like_contains("a_b") == "%a\\_b%"
    assert like_contains("a\\b") == "%a\\\\b%"


async def test_percent_sign_is_literal_not_a_match_everything_wildcard(client, admin_headers):
    store_id, cat_id = await _setup(client, admin_headers)
    await _coupon(client, admin_headers, store_id, cat_id, "Save 50% today")
    await _coupon(client, admin_headers, store_id, cat_id, "Plain offer")
    assert await _search(client, "%") == {"Save 50% today"}
    assert await _search(client, "50%") == {"Save 50% today"}


async def test_underscore_is_literal(client, admin_headers):
    store_id, cat_id = await _setup(client, admin_headers)
    await _coupon(client, admin_headers, store_id, cat_id, "Code a_b works")
    await _coupon(client, admin_headers, store_id, cat_id, "Code axb works")
    assert await _search(client, "a_b") == {"Code a_b works"}


# ------------------------------------------------------------ search semantics kept


async def test_search_still_matches_store_and_category_names(client, admin_headers):
    nike, shoes = await _setup(client, admin_headers, "Nikeworld", "Footwear")
    other, _ = await _setup(client, admin_headers, "Elsewhere", "Gadgets")
    await _coupon(client, admin_headers, nike, shoes, "Alpha")
    await _coupon(client, admin_headers, other, shoes, "Bravo")
    await _coupon(
        client,
        admin_headers,
        other,
        (await _setup(client, admin_headers, "X", "Gizmos"))[1],
        "Charlie",
    )
    # matched through the store name only
    assert await _search(client, "nikeworld") == {"Alpha"}
    # matched through the category name only
    assert await _search(client, "footwear") == {"Alpha", "Bravo"}


async def test_every_term_must_match_somewhere(client, admin_headers):
    nike, shoes = await _setup(client, admin_headers, "Nikeworld", "Footwear")
    _, gadgets = await _setup(client, admin_headers, "Elsewhere", "Gadgets")
    await _coupon(client, admin_headers, nike, shoes, "Alpha")
    await _coupon(client, admin_headers, nike, gadgets, "Bravo")
    # "nikeworld" (store) AND "footwear" (category): only Alpha has both
    assert await _search(client, "nikeworld footwear") == {"Alpha"}


async def test_generic_intent_words_are_ignored_but_store_still_found(client, admin_headers):
    nike, shoes = await _setup(client, admin_headers, "Nikeworld", "Footwear")
    await _coupon(client, admin_headers, nike, shoes, "Alpha")
    assert await _search(client, "nikeworld promo code") == {"Alpha"}


# ------------------------------------------------------------ input bounds


@pytest.mark.parametrize("path", ["coupons", "stores", "products"])
async def test_search_parameter_is_length_bounded(client, path):
    ok = await client.get(f"{API}/{path}", params={"search": "x" * 100})
    too_long = await client.get(f"{API}/{path}", params={"search": "x" * 101})
    assert ok.status_code == 200
    assert too_long.status_code == 422


async def test_suggest_is_rate_limited(client):
    statuses = [
        (await client.get(f"{API}/search/suggest", params={"q": "a"})).status_code
        for _ in range(125)
    ]
    assert statuses[:120] == [200] * 120
    assert 429 in statuses[120:]


# ------------------------------------------------------------ destination total


async def test_destination_covers_every_live_offer_and_counts_them(client, admin_headers):
    """Code, no code, and an empty-string code must each land in exactly one bucket.

    The store page used to merge in a second listing because offers were thought to fall
    between the two buckets. They do not (`code IS NOT NULL` / `code IS NULL`), and this pins
    that so the page can rely on the destination payload alone.
    """
    store_id, cat_id = await _setup(client, admin_headers, "Totalshop", "Totals")
    await _coupon(client, admin_headers, store_id, cat_id, "With code")
    await _coupon(client, admin_headers, store_id, cat_id, "No code deal", code=None)
    await _coupon(client, admin_headers, store_id, cat_id, "Blank code deal", code="")
    slug = (await client.get(f"{API}/stores/{store_id}")).json()["slug"]
    body = (await client.get(f"{API}/stores/by-slug/{slug}/destination")).json()
    listed = {c["title"] for c in body["deals"]} | {c["title"] for c in body["coupons"]}
    assert listed == {"With code", "No code deal", "Blank code deal"}
    assert len(body["deals"]) + len(body["coupons"]) == 3  # no offer in both buckets
    assert body["offer_total"] == 3


# ------------------------------------------------------------ cache headers (pure)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/v1/coupons", OFFERS),
        ("/api/v1/coupons/by-slug/some-offer", OFFERS),
        ("/api/v1/stores/by-slug/amazon/destination", OFFERS),
        ("/api/v1/products", OFFERS),
        ("/api/v1/products/abc/price-history", OFFERS),
        ("/api/v1/stores", CATALOGUE),
        ("/api/v1/stores/by-slug/amazon", CATALOGUE),
        ("/api/v1/stores/markets", CATALOGUE),
        ("/api/v1/categories", CATALOGUE),
        ("/api/v1/editorial/homepage", CATALOGUE),
        ("/api/v1/editorial/guide/how-to-save", CATALOGUE),
        ("/api/v1/sales/diwali/2026", CATALOGUE),
    ],
)
def test_cacheable_paths(path, expected):
    assert cache_control_for(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        f"/api/v1/coupons/{uuid.uuid4()}",  # live vote counts
        f"/api/v1/coupons/{uuid.uuid4()}/go",  # tracked redirect
        f"/api/v1/coupons/{uuid.uuid4()}/verification-history",
        f"/api/v1/ads/{uuid.uuid4()}/go",
        "/api/v1/ads",
        "/api/v1/stores/autocomplete",
        "/api/v1/search/suggest",
        "/api/v1/editorial/manage",
        f"/api/v1/editorial/id/{uuid.uuid4()}",
        "/api/v1/me/saved-coupons",
        "/api/v1/auth/me",
        "/api/v1/account/anything",
        "/api/v1/admin/analytics/overview",
        "/api/v1/personalization/x",
        "/health",
        "/metrics",
    ],
)
def test_everything_else_is_not_cacheable(path):
    assert cache_control_for(path) is None


# ------------------------------------------------------------ cache headers (end to end)


async def test_public_list_gets_cache_control(client):
    resp = await client.get(f"{API}/categories")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == CATALOGUE


async def test_not_found_is_never_cached(client):
    resp = await client.get(f"{API}/stores/by-slug/does-not-exist")
    assert resp.status_code == 404
    assert "cache-control" not in resp.headers


async def test_authorization_header_disables_caching(client, admin_headers):
    resp = await client.get(f"{API}/categories", headers=admin_headers)
    assert resp.status_code == 200
    assert "cache-control" not in resp.headers


async def test_unlisted_routes_are_untouched(client):
    resp = await client.get("/health")
    assert "cache-control" not in resp.headers
