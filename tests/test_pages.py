API = "/api/v1/pages"


async def test_page_crud(client, admin_headers):
    created = await client.post(
        API,
        json={"title": "About Us", "content": "<p>Hello</p>", "meta_description": "About"},
        headers=admin_headers,
    )
    assert created.status_code == 201
    page = created.json()
    assert page["slug"] == "about-us"

    assert (await client.get(f"{API}/by-slug/about-us")).json()["content"] == "<p>Hello</p>"

    updated = await client.patch(
        f"{API}/{page['id']}", json={"title": "Who We Are"}, headers=admin_headers
    )
    assert updated.json()["slug"] == "who-we-are"

    assert (await client.delete(f"{API}/{page['id']}", headers=admin_headers)).status_code == 204


async def test_unpublished_pages_hidden_by_default(client, admin_headers):
    await client.post(API, json={"title": "Live", "content": "x"}, headers=admin_headers)
    await client.post(
        API, json={"title": "Draft", "content": "x", "is_published": False}, headers=admin_headers
    )
    assert (await client.get(API)).json()["total"] == 1
    assert (await client.get(API, params={"published_only": False})).json()["total"] == 2


async def test_page_rbac(client, user_headers, editor_headers):
    body = {"title": "Terms", "content": "..."}
    assert (await client.post(API, json=body)).status_code == 401
    assert (await client.post(API, json=body, headers=user_headers)).status_code == 403
    assert (await client.post(API, json=body, headers=editor_headers)).status_code == 201
