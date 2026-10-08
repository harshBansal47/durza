import uuid

API = "/api/v1/categories"


async def test_create_list_get_update_delete(client, admin_headers):
    created = await client.post(API, json={"name": "Electronics"}, headers=admin_headers)
    assert created.status_code == 201
    cat = created.json()
    assert cat["slug"] == "electronics"

    listing = await client.get(API)  # public
    assert listing.status_code == 200
    assert listing.json()["total"] == 1

    assert (await client.get(f"{API}/{cat['id']}")).json()["name"] == "Electronics"
    assert (await client.get(f"{API}/by-slug/electronics")).json()["id"] == cat["id"]

    updated = await client.patch(
        f"{API}/{cat['id']}", json={"name": "Gadgets"}, headers=admin_headers
    )
    assert updated.status_code == 200
    assert updated.json()["slug"] == "gadgets"  # slug follows the name

    deleted = await client.delete(f"{API}/{cat['id']}", headers=admin_headers)
    assert deleted.status_code == 204
    assert (await client.get(f"{API}/{cat['id']}")).status_code == 404


async def test_duplicate_names_get_unique_slugs(client, admin_headers):
    first = await client.post(API, json={"name": "Fashion"}, headers=admin_headers)
    second = await client.post(API, json={"name": "Fashion"}, headers=admin_headers)
    assert first.json()["slug"] == "fashion"
    assert second.json()["slug"] == "fashion-2"


async def test_subcategories_and_parent_filter(client, admin_headers):
    parent = (await client.post(API, json={"name": "Home"}, headers=admin_headers)).json()
    await client.post(
        API, json={"name": "Kitchen", "parent_id": parent["id"]}, headers=admin_headers
    )
    await client.post(API, json={"name": "Garden"}, headers=admin_headers)

    children = await client.get(API, params={"parent_id": parent["id"]})
    assert children.json()["total"] == 1
    assert children.json()["items"][0]["name"] == "Kitchen"


async def test_unknown_parent_returns_404(client, admin_headers):
    resp = await client.post(
        API, json={"name": "Orphan", "parent_id": str(uuid.uuid4())}, headers=admin_headers
    )
    assert resp.status_code == 404


async def test_category_cannot_be_its_own_parent(client, admin_headers):
    cat = (await client.post(API, json={"name": "Loop"}, headers=admin_headers)).json()
    resp = await client.patch(
        f"{API}/{cat['id']}", json={"parent_id": cat["id"]}, headers=admin_headers
    )
    assert resp.status_code == 400


async def test_pagination(client, admin_headers):
    for i in range(5):
        await client.post(API, json={"name": f"Cat {i}"}, headers=admin_headers)
    page = await client.get(API, params={"skip": 2, "limit": 2})
    body = page.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2


async def test_rbac(client, user_headers, editor_headers, admin_headers):
    payload = {"name": "Restricted"}
    assert (await client.post(API, json=payload)).status_code == 401
    assert (await client.post(API, json=payload, headers=user_headers)).status_code == 403

    created = await client.post(API, json=payload, headers=editor_headers)
    assert created.status_code == 201  # editors may create
    cat_id = created.json()["id"]

    assert (await client.delete(f"{API}/{cat_id}", headers=editor_headers)).status_code == 403
    assert (await client.delete(f"{API}/{cat_id}", headers=admin_headers)).status_code == 204


async def test_category_autocomplete(client, admin_headers):
    for name in ("Electronics", "Electronics Accessories", "Fashion", "Home & Garden"):
        await client.post(API, json={"name": name}, headers=admin_headers)

    hits = await client.get(f"{API}/autocomplete", params={"q": "electron"})
    assert hits.status_code == 200
    data = hits.json()
    assert isinstance(data, list)
    assert "Electronics" in data
    assert "Electronics Accessories" in data
    assert "Fashion" not in data


async def test_category_autocomplete_limit(client, admin_headers):
    for i in range(15):
        await client.post(API, json={"name": f"Category {i}"}, headers=admin_headers)

    hits = await client.get(f"{API}/autocomplete", params={"q": "category", "limit": 5})
    assert hits.status_code == 200
    data = hits.json()
    assert len(data) == 5
