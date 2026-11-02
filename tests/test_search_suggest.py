from sqlalchemy.ext.asyncio import AsyncSession

from tests.test_query_budget import count_queries

API = "/api/v1"


async def _seed(client, headers):
    await client.post(f"{API}/stores", json={"name": "Nike"}, headers=headers)
    await client.post(f"{API}/stores", json={"name": "Nikon"}, headers=headers)
    await client.post(f"{API}/categories", json={"name": "Nightwear"}, headers=headers)


async def test_suggest_matches_the_three_separate_endpoints(client, admin_headers):
    await _seed(client, admin_headers)
    combined = (await client.get(f"{API}/search/suggest", params={"q": "ni", "limit": 5})).json()
    separate = {
        "stores": (
            await client.get(f"{API}/stores/autocomplete", params={"q": "ni", "limit": 5})
        ).json(),
        "categories": (
            await client.get(f"{API}/categories/autocomplete", params={"q": "ni", "limit": 5})
        ).json(),
        "products": (
            await client.get(f"{API}/products/autocomplete", params={"q": "ni", "limit": 5})
        ).json(),
    }
    assert combined == separate
    assert sorted(combined["stores"]) == ["Nike", "Nikon"]
    assert combined["categories"] == ["Nightwear"]


async def test_suggest_is_one_request_and_one_database_session(client, admin_headers, engine):
    await _seed(client, admin_headers)
    async with count_queries(engine) as statements:
        resp = await client.get(f"{API}/search/suggest", params={"q": "ni"})
    assert resp.status_code == 200
    print(f"\n[suggest] 1 request, {len(statements)} statements (was 3 requests)")
    assert len(statements) <= 3


async def test_suggest_returns_empty_groups_when_nothing_matches(client):
    resp = await client.get(f"{API}/search/suggest", params={"q": "zzzz"})
    assert resp.json() == {"stores": [], "categories": [], "products": []}


async def test_suggest_validates_like_the_individual_endpoints(client):
    assert (await client.get(f"{API}/search/suggest")).status_code == 422
    assert (await client.get(f"{API}/search/suggest", params={"q": ""})).status_code == 422
    assert (await client.get(f"{API}/search/suggest", params={"q": "x" * 101})).status_code == 422
    assert (
        await client.get(f"{API}/search/suggest", params={"q": "a", "limit": 21})
    ).status_code == 422


async def test_one_failing_source_does_not_blank_the_others(client, admin_headers, monkeypatch):
    await _seed(client, admin_headers)
    from app.services import category_service

    async def boom(db: AsyncSession, query: str, limit: int = 10):
        raise RuntimeError("category lookup down")

    monkeypatch.setattr(category_service, "autocomplete_categories", boom)
    resp = await client.get(f"{API}/search/suggest", params={"q": "ni"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["categories"] == []
    assert sorted(body["stores"]) == ["Nike", "Nikon"]
