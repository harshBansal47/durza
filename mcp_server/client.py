"""Thin async HTTP client over Savevero's public JSON API.

Deliberately separate from the FastAPI app's own service layer: this talks to
the API the same way any external agent would, over HTTP, using only public
endpoints. It never touches the database directly.
"""

import os
from typing import Any

import httpx

API_URL = os.environ.get("SAVEVERO_API_URL", "http://localhost:8000/api/v1")


class SaveveroClient:
    def __init__(
        self, base_url: str = API_URL, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self._base_url, transport=self._transport, timeout=10)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        async with self._client() as client:
            resp = await client.get(
                path, params={k: v for k, v in (params or {}).items() if v is not None}
            )
            resp.raise_for_status()
            return resp.json()

    async def _post(self, path: str, json: dict[str, Any]) -> Any:
        async with self._client() as client:
            resp = await client.post(path, json=json)
            resp.raise_for_status()
            return resp.json()

    async def search_coupons(
        self,
        query: str | None = None,
        store_slug: str | None = None,
        category_slug: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        store_id = None
        if store_slug:
            store = await self._get(f"/stores/by-slug/{store_slug}")
            store_id = store["id"]

        category_id = None
        if category_slug:
            category = await self._get(f"/categories/by-slug/{category_slug}")
            category_id = category["id"]

        page = await self._get(
            "/coupons",
            {"search": query, "store_id": store_id, "category_id": category_id, "limit": limit},
        )
        return [self._summarize_coupon(c) for c in page["items"]]

    async def get_coupon(self, slug: str) -> dict[str, Any]:
        coupon = await self._get(f"/coupons/by-slug/{slug}")
        return self._summarize_coupon(coupon, full=True)

    async def get_store(self, slug: str) -> dict[str, Any]:
        store = await self._get(f"/stores/by-slug/{slug}")
        return {
            "name": store["name"],
            "slug": store["slug"],
            "website_url": store["website_url"],
            "description": store["description"],
            "commission_disclosure": store["commission_disclosure"],
        }

    async def list_categories(self) -> list[dict[str, Any]]:
        page = await self._get("/categories", {"limit": 100})
        return [{"name": c["name"], "slug": c["slug"]} for c in page["items"]]

    async def report_verification(self, coupon_slug: str, worked: bool) -> dict[str, Any]:
        coupon = await self._get(f"/coupons/by-slug/{coupon_slug}")
        return await self._post(f"/coupons/{coupon['id']}/verify", {"worked": worked})

    def _summarize_coupon(self, coupon: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
        summary = {
            "title": coupon["title"],
            "slug": coupon["slug"],
            "code": coupon["code"],
            "discount_type": coupon["discount_type"],
            "discount_value": coupon["discount_value"],
            "success_rate": coupon["success_rate"],
            "verification_count": coupon["success_count"] + coupon["fail_count"],
            "last_verified_at": coupon["last_verified_at"],
            "expires_at": coupon["expires_at"],
            "redeem_url": f"{self._base_url}/coupons/{coupon['id']}/go",
        }
        if full:
            summary["description"] = coupon["description"]
        return summary
