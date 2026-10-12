API = "/api/v1/stores"


async def test_store_crud(client, admin_headers):
    created = await client.post(
        API,
        json={"name": "Amazon", "website_url": "https://amazon.com"},
        headers=admin_headers,
    )
    assert created.status_code == 201
    store = created.json()
    assert store["slug"] == "amazon"
    assert store["is_active"] is True

    assert (await client.get(f"{API}/by-slug/amazon")).json()["id"] == store["id"]

    updated = await client.patch(
        f"{API}/{store['id']}", json={"is_active": False}, headers=admin_headers
    )
    assert updated.json()["is_active"] is False

    assert (await client.delete(f"{API}/{store['id']}", headers=admin_headers)).status_code == 204
    assert (await client.get(f"{API}/{store['id']}")).status_code == 404


async def test_store_search(client, admin_headers):
    for name in ("Amazon", "AliExpress", "eBay"):
        await client.post(API, json={"name": name}, headers=admin_headers)

    hits = await client.get(API, params={"search": "ali"})
    assert hits.json()["total"] == 1
    assert hits.json()["items"][0]["name"] == "AliExpress"


async def test_store_rbac(client, user_headers, editor_headers):
    payload = {"name": "Nope"}
    assert (await client.post(API, json=payload)).status_code == 401
    assert (await client.post(API, json=payload, headers=user_headers)).status_code == 403
    assert (await client.post(API, json=payload, headers=editor_headers)).status_code == 201


async def test_commission_disclosure_round_trip(client, admin_headers):
    created = await client.post(
        API,
        json={"name": "Nike", "commission_disclosure": "We earn 5% from Nike on this link."},
        headers=admin_headers,
    )
    assert created.json()["commission_disclosure"] == "We earn 5% from Nike on this link."

    no_disclosure = (
        await client.post(API, json={"name": "Local Shop"}, headers=admin_headers)
    ).json()
    assert no_disclosure["commission_disclosure"] is None


async def test_store_autocomplete(client, admin_headers):
    for name in ("Amazon", "AliExpress", "eBay", "Amazon India"):
        await client.post(API, json={"name": name}, headers=admin_headers)

    hits = await client.get(f"{API}/autocomplete", params={"q": "ama"})
    assert hits.status_code == 200
    data = hits.json()
    assert isinstance(data, list)
    assert "Amazon" in data
    assert "Amazon India" in data
    assert "AliExpress" not in data
    assert "eBay" not in data


async def test_store_autocomplete_limit(client, admin_headers):
    for i in range(15):
        await client.post(API, json={"name": f"Store {i}"}, headers=admin_headers)

    hits = await client.get(f"{API}/autocomplete", params={"q": "store", "limit": 5})
    assert hits.status_code == 200
    data = hits.json()
    assert len(data) == 5


async def test_store_market_fields_round_trip(client, admin_headers):
    """Durza lists per country, so a store carries its market and currency."""
    created = await client.post(
        API,
        json={
            "name": "Flipkart",
            "country_code": "in",
            "currency": "inr",
        },
        headers=admin_headers,
    )
    assert created.status_code == 201
    store = created.json()
    # Codes are normalised to upper case on the way in.
    assert store["country_code"] == "IN"
    assert store["currency"] == "INR"

    updated = await client.patch(
        f"{API}/{store['id']}",
        json={"country_code": "de", "currency": "eur"},
        headers=admin_headers,
    )
    assert updated.json()["country_code"] == "DE"
    assert updated.json()["currency"] == "EUR"


async def test_store_rejects_bad_market_codes(client, admin_headers):
    bad_country = await client.post(
        API, json={"name": "Bad", "country_code": "IND"}, headers=admin_headers
    )
    assert bad_country.status_code == 422

    bad_currency = await client.post(
        API, json={"name": "Bad2", "currency": "RUPEE"}, headers=admin_headers
    )
    assert bad_currency.status_code == 422


async def test_list_stores_filtered_by_country(client, admin_headers):
    """Country filter scopes to that market but keeps global stores visible —
    otherwise Amazon would vanish from every country listing."""
    for name, country in (
        ("Amazon", None),
        ("Flipkart", "IN"),
        ("Zalando", "DE"),
    ):
        await client.post(API, json={"name": name, "country_code": country}, headers=admin_headers)

    india = await client.get(API, params={"country_code": "IN"})
    names = {item["name"] for item in india.json()["items"]}
    assert names == {"Amazon", "Flipkart"}

    germany = await client.get(API, params={"country_code": "DE"})
    assert {item["name"] for item in germany.json()["items"]} == {"Amazon", "Zalando"}

    unfiltered = await client.get(API)
    assert unfiltered.json()["total"] == 3


async def test_list_markets(client, admin_headers):
    await client.post(API, json={"name": "Flipkart", "country_code": "IN"}, headers=admin_headers)
    await client.post(API, json={"name": "Zalando", "country_code": "DE"}, headers=admin_headers)
    await client.post(API, json={"name": "Amazon"}, headers=admin_headers)  # global

    markets = await client.get(f"{API}/markets")
    assert markets.status_code == 200
    assert sorted(markets.json()) == ["DE", "IN"]

    # Inactive stores drop out of the market list.
    global_store = (await client.get(API, params={"search": "Amazon"})).json()["items"][0]
    await client.patch(
        f"{API}/{global_store['id']}",
        json={"country_code": "GB"},
        headers=admin_headers,
    )
    await client.patch(
        f"{API}/{global_store['id']}", json={"is_active": False}, headers=admin_headers
    )
    markets = await client.get(f"{API}/markets")
    assert "GB" not in markets.json()
