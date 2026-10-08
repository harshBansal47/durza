import pytest


async def _seed_deal(client, admin_headers):
    store = (
        await client.post(
            "/api/v1/stores",
            json={"name": "Amazon", "commission_disclosure": "We earn ~4% here."},
            headers=admin_headers,
        )
    ).json()
    category = (
        await client.post("/api/v1/categories", json={"name": "Electronics"}, headers=admin_headers)
    ).json()
    coupon = (
        await client.post(
            "/api/v1/coupons",
            json={
                "title": "20% off headphones",
                "code": "SAVE20",
                "discount_type": "percentage",
                "discount_value": 20,
                "store_id": store["id"],
                "category_id": category["id"],
                "destination_url": "https://amazon.com/deal",
            },
            headers=admin_headers,
        )
    ).json()
    return store, category, coupon


async def test_client_search_coupons_by_query(client, admin_headers, mcp_client):
    await _seed_deal(client, admin_headers)
    results = await mcp_client.search_coupons(query="headphones")
    assert len(results) == 1
    assert results[0]["code"] == "SAVE20"
    assert results[0]["success_rate"] is None  # not yet reported on
    assert "redeem_url" in results[0]
    assert "destination_url" not in results[0]  # same rule as the public JSON API


async def test_client_search_coupons_by_store_and_category_slug(client, admin_headers, mcp_client):
    await _seed_deal(client, admin_headers)

    by_store = await mcp_client.search_coupons(store_slug="amazon")
    assert len(by_store) == 1

    by_category = await mcp_client.search_coupons(category_slug="electronics")
    assert len(by_category) == 1


async def test_client_search_coupons_unknown_store_slug_raises(client, admin_headers, mcp_client):
    await _seed_deal(client, admin_headers)

    import httpx

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await mcp_client.search_coupons(store_slug="does-not-exist")
    assert exc_info.value.response.status_code == 404


async def test_client_get_coupon_includes_description_and_redeem_url(
    client, admin_headers, mcp_client
):
    _, _, coupon = await _seed_deal(client, admin_headers)
    detail = await mcp_client.get_coupon(coupon["slug"])
    assert detail["title"] == "20% off headphones"
    assert detail["redeem_url"].endswith(f"/coupons/{coupon['id']}/go")
    assert "description" in detail  # only get_coupon (not search) includes this


async def test_client_get_store_includes_disclosure(client, admin_headers, mcp_client):
    await _seed_deal(client, admin_headers)
    store = await mcp_client.get_store("amazon")
    assert store["commission_disclosure"] == "We earn ~4% here."


async def test_client_list_categories(client, admin_headers, mcp_client):
    await _seed_deal(client, admin_headers)
    categories = await mcp_client.list_categories()
    assert {"name": "Electronics", "slug": "electronics"} in categories


async def test_client_report_verification_updates_success_rate(client, admin_headers, mcp_client):
    _, _, coupon = await _seed_deal(client, admin_headers)
    result = await mcp_client.report_verification(coupon["slug"], True)
    assert result["success_count"] == 1
    assert result["success_rate"] == 1.0

    # The same rate-limit rules apply to agent-submitted reports as human ones.
    import httpx

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await mcp_client.report_verification(coupon["slug"], False)
    assert exc_info.value.response.status_code == 429


async def test_server_registers_all_five_tools():
    # `mcp` is an optional extra (`pip install -e ".[mcp]"`), so the rest of the
    # suite must not depend on it. Skip rather than fail: CI installs the dev
    # extra only, and a missing optional integration is not a regression.
    pytest.importorskip("mcp", reason="requires the optional 'mcp' extra")

    from mcp_server.server import mcp

    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "search_coupons",
        "get_coupon",
        "get_store",
        "list_categories",
        "report_coupon_result",
    }
    # Every tool needs a real docstring — that's the schema an agent actually sees.
    assert all(t.description for t in tools)
