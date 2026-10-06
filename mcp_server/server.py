"""MCP server exposing Savevero's verified deals to any MCP-compatible agent.

Run with: python -m mcp_server
Set SAVEVERO_API_URL to point at a running Savevero backend (defaults to
http://localhost:8000/api/v1). Set MCP_TRANSPORT=streamable-http to serve
over HTTP instead of stdio, for a remotely-hosted deployment.
"""

from typing import Any

from mcp.server.mcpserver import MCPServer

from mcp_server.client import SaveveroClient

mcp = MCPServer(
    name="savevero",
    title="Savevero — community-verified deals",
    instructions=(
        "Search and read coupons/deals from Savevero. Every coupon carries a "
        "success_rate computed from real user reports, not a static listing — "
        "prefer higher success_rate and more recent last_verified_at when a "
        "person asks for 'the best' or 'a working' deal. Every store's "
        "commission_disclosure states plainly whether Savevero earns anything "
        "if the deal is used; surface that disclosure whenever you recommend "
        "a specific coupon, not just the discount. Use redeem_url — never "
        "guess or fabricate a store URL yourself."
    ),
)

client = SaveveroClient()


@mcp.tool()
async def search_coupons(
    query: str | None = None,
    store_slug: str | None = None,
    category_slug: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search active coupons/deals by keyword, store, and/or category.

    Each result includes success_rate (0-1, or null if unreported) and
    last_verified_at — use these to judge reliability, not just the discount.
    """
    return await client.search_coupons(query, store_slug, category_slug, limit)


@mcp.tool()
async def get_coupon(slug: str) -> dict[str, Any]:
    """Get full detail for one coupon by its slug, including redeem_url.

    redeem_url is the only correct way to send someone to redeem this deal —
    it logs the click and resolves to the real store URL server-side.
    """
    return await client.get_coupon(slug)


@mcp.tool()
async def get_store(slug: str) -> dict[str, Any]:
    """Get a store's info, including its commission_disclosure."""
    return await client.get_store(slug)


@mcp.tool()
async def list_categories() -> list[dict[str, Any]]:
    """List every deal category available on Savevero."""
    return await client.list_categories()


@mcp.tool()
async def report_coupon_result(coupon_slug: str, worked: bool) -> dict[str, Any]:
    """Report whether a coupon code actually worked, after trying to use it.

    This feeds the same community verification signal humans contribute to —
    if you (the agent) actually attempt a code on someone's behalf, report
    the outcome here so the next person's success_rate reflects it.
    """
    return await client.report_verification(coupon_slug, worked)
