"""Query-count budgets for the hottest public read paths.

These count the SQL statements a request actually issues (SQLAlchemy's
`before_cursor_execute`), so a regression such as an N+1 or an eager load nobody
reads shows up as a number rather than as a slow page weeks later.

Counts, not timings: statement count is deterministic on SQLite and carries over
to Postgres, whereas latency measured against in-memory SQLite would say nothing
about production. Run with `-s` to print the measured numbers.
"""

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import event

API = "/api/v1"


@asynccontextmanager
async def count_queries(engine):
    """Collect every statement issued on `engine` while the block runs."""
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)


async def _seed_store_with_offers(client, headers, *, offers=6, articles=8):
    store = (await client.post(f"{API}/stores", json={"name": "Hostinger"}, headers=headers)).json()
    category = (
        await client.post(f"{API}/categories", json={"name": "Hosting"}, headers=headers)
    ).json()
    for i in range(offers):
        resp = await client.post(
            f"{API}/coupons",
            headers=headers,
            json={
                "title": f"Offer {i}",
                "code": f"CODE{i}" if i % 2 == 0 else None,
                "discount_type": "percentage",
                "discount_value": 10 + i,
                "store_id": store["id"],
                "category_id": category["id"],
                "destination_url": f"https://example.com/{i}",
            },
        )
        assert resp.status_code == 201, resp.text
    for i in range(articles):
        resp = await client.post(
            f"{API}/editorial",
            headers=headers,
            json={
                "content_type": "comparison",
                "title": f"Hostinger comparison {i}",
                "status": "published",
                "summary": "Compare providers.",
                "content_blocks": [{"type": "paragraph", "text": "Details."}],
                "entity_links": [{"store_id": store["id"]}],
            },
        )
        assert resp.status_code == 201, resp.text
    return store, category


async def test_coupon_list_query_budget(client, admin_headers, engine):
    await _seed_store_with_offers(client, admin_headers, offers=6, articles=0)
    async with count_queries(engine) as statements:
        resp = await client.get(f"{API}/coupons", params={"limit": 20})
    assert resp.status_code == 200
    assert resp.json()["total"] == 6
    print(f"\n[budget] GET /coupons (6 rows): {len(statements)} statements")
    # count + page. The eager `sources` load was a third statement nobody used.
    assert len(statements) <= 2, statements


async def test_coupon_get_query_budget(client, admin_headers, engine):
    await _seed_store_with_offers(client, admin_headers, offers=1, articles=0)
    listing = (await client.get(f"{API}/coupons")).json()["items"][0]
    async with count_queries(engine) as statements:
        by_id = await client.get(f"{API}/coupons/{listing['id']}")
        by_slug = await client.get(f"{API}/coupons/by-slug/{listing['slug']}")
    assert by_id.status_code == by_slug.status_code == 200
    print(f"\n[budget] GET /coupons/{{id}} + /by-slug: {len(statements)} statements")
    assert len(statements) <= 2, statements


async def test_store_destination_query_budget(client, admin_headers, engine):
    await _seed_store_with_offers(client, admin_headers, offers=6, articles=8)
    async with count_queries(engine) as statements:
        resp = await client.get(f"{API}/stores/by-slug/hostinger/destination")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["related_content"]) == 8
    assert len(data["deals"]) + len(data["coupons"]) == 6
    print(f"\n[budget] GET /stores/.../destination (8 articles): {len(statements)} statements")
    # Constant in the number of articles: it was 3 per article before.
    assert len(statements) <= 14, statements


@pytest.mark.parametrize("articles", [2, 8])
async def test_store_destination_does_not_scale_with_articles(
    client, admin_headers, engine, articles
):
    await _seed_store_with_offers(client, admin_headers, offers=2, articles=articles)
    async with count_queries(engine) as statements:
        resp = await client.get(f"{API}/stores/by-slug/hostinger/destination")
    assert resp.status_code == 200
    print(f"\n[budget] destination with {articles} articles: {len(statements)} statements")
    assert len(statements) <= 14, statements
