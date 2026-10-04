"""Awin (Affiliate Window) API adapter.

Fetches voucher/deal data from the Awin API and converts to RawOffers.

Awin API docs: https://api.awin.com/
- Base URL: https://api.awin.com
- Auth: API key in header or query param
- Voucher feed: /publishers/{publisherId}/advertisers/{advertiserId}/vouchers
- Deals feed: /publishers/{publisherId}/advertisers/{advertiserId}/deals
- Pagination: page/limit params

Config schema:
{
    "publisher_id": 12345,
    "advertiser_ids": [123, 456],  # optional, defaults to all approved
    "program_status": "active",   # "active" | "pending" | "all"
    "deal_types": ["coupon", "deal", "sale"],  # what to fetch
    "currency": "USD",            # filter by currency
    "regions": ["US", "GB", "DE", "FR"], # filter by region
    "page_size": 100              # items per page
}

**The API key is not in that dict.** It arrives as a resolved
`SourceCredentials`, because the previous version of this adapter read
`config["api_key"]` — which meant the key was a column value in `sources.config`,
rendered in the SQLAdmin source detail view and readable by anyone who could
export that table. The key now lives in an environment variable named by
`Source.credential_ref` ("AWIN_API_KEY"), and nothing in the database holds it.
"""

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import httpx

from app.ingestion.base import PartialFetchError, RawOffer
from app.ingestion.credentials import SourceCredentials
from app.ingestion.retry import with_retries

if TYPE_CHECKING:
    from app.models.ingestion_run import SyncMode

_AWIN_BASE = "https://api.awin.com"
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_USER_AGENT = "CouponbaseBot/1.0 (+https://couponbase.example.com/bot)"


class AwinSource:
    """Fetches vouchers/deals from the Awin Publisher API.

    Awin's voucher/deal endpoints return offers with fields like:
    - title, description, voucherCode, startDate, endDate
    - advertiserName, advertiserId, deepLink (destination URL)
    - discountAmount, discountType, discountPercentage
    - currency, regions, categories
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def stream(
        self,
        config: dict[str, Any],
        *,
        mode: "SyncMode | None" = None,
        credentials: SourceCredentials | None = None,
    ) -> AsyncIterator[tuple[RawOffer, int]]:
        """Yield `(offer, pages_fetched)` page by page.

        Streaming rather than `fetch` for one reason: **partial is a real state
        and this adapter can reach it.** A publisher with 30 advertisers at 100
        per page is 30+ requests; if request 18 fails after retries, a
        `fetch`-based implementation loses all 17 pages that succeeded. Yielding
        means those 17 pages are already in the database when `PartialFetchError`
        fires, and the run is recorded as `partial` rather than `failed` — which
        is the difference between "we got 1,700 offers and stopped" and "we got
        nothing, retry everything".

        `fetch` is kept below as the non-streaming fallback for callers that want
        a plain list.
        """
        # `only_value` rather than `get("AWIN_API_KEY")`: the adapter asks for
        # "the one credential this source references" and does not care what the
        # variable is called. Renaming it in the deployment config must not
        # require a code change here.
        if credentials is None or not credentials.values:
            raise ValueError(
                "awin source requires an API key: set Source.credential_ref to the "
                "name of the environment variable holding it"
            )
        api_key = credentials.only_value()
        publisher_id = config.get("publisher_id")
        if not publisher_id:
            raise ValueError("awin source config requires 'publisher_id'")

        advertiser_ids = config.get("advertiser_ids") or [None]
        deal_types = config.get("deal_types", ["coupon", "deal", "sale"])
        page_size = config.get("page_size", 100)
        base_params = {"programStatus": config.get("program_status", "active")}
        if config.get("currency"):
            base_params["currency"] = config["currency"]
        if config.get("regions"):
            base_params["regions"] = ",".join(config["regions"])

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=_TIMEOUT)

        # Sent per request rather than baked into the client's default headers.
        # With a client injected — which is how every test runs — client-level
        # headers would be whatever the caller happened to configure, and the
        # request would go out unauthenticated. `owns_client` used to gate this
        # and only the production path got a key.
        headers = {
            "User-Agent": _USER_AGENT,
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }

        async def _page(endpoint: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            response = await client.get(
                f"{_AWIN_BASE}{endpoint}", params=params, headers=headers
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                # A shape change here is a provider-side breaking change. Failing
                # the page rather than silently returning nothing is what keeps it
                # visible instead of showing up as an empty catalogue.
                raise TypeError(
                    f"awin response from {endpoint} has no 'results' list; "
                    f"the endpoint shape may have changed"
                )
            return payload["results"]

        pages = 0
        # Collects a failure so the loop can finish handing over everything it
        # already has before surfacing it.
        failure: Exception | None = None

        try:
            for resource in self._resources(deal_types):
                for advertiser_id in advertiser_ids:
                    if failure is not None:
                        break
                    endpoint = self._endpoint(resource, publisher_id, advertiser_id)
                    params = {**base_params, **resource["params"]}
                    page = 0
                    while True:
                        try:
                            results = await with_retries(
                                lambda ep=endpoint, p={**params, "page": page, "pageSize": page_size}: _page(ep, p),
                                retryable=(httpx.HTTPError, TypeError),
                            )
                        except (httpx.HTTPError, TypeError) as exc:
                            failure = exc
                            break
                        if not results:
                            break
                        pages += 1
                        for item in results:
                            mapped = resource["map"](item)
                            if mapped is not None:
                                yield mapped, pages
                        page += 1
                        # Keep going until a page comes back *empty*. Treating a
                        # short page as the last one would save one request per
                        # advertiser and is a real hazard: a provider that
                        # truncates mid-catalogue would make this loop stop early,
                        # the adapter would return normally rather than raising
                        # `PartialFetchError`, and the pipeline would then conclude
                        # the source no longer lists the coupons it never
                        # fetched. One extra request is the cheap option.
        finally:
            if owns_client:
                await client.aclose()

        if failure is not None:
            # Everything yielded above is already committed by the pipeline's
            # batching. Naming the endpoint and not the exception message keeps a
            # URL with an embedded key out of the run row.
            raise PartialFetchError(
                f"awin pagination stopped at page {pages} of {resource['name']}: "
                f"{type(failure).__name__}",
                pages_completed=pages,
            ) from failure

    def _resources(self, deal_types: list[str]) -> list[dict[str, Any]]:
        """Which Awin feeds to read, and how each is filtered.

        Awin splits coded vouchers and code-less deals across two endpoints with
        different filter parameters, so "what to fetch" is expressed as a list of
        resources rather than as a set of `if` branches inside the pagination loop
        — otherwise the two endpoints drift apart and one of them silently stops
        being filtered.
        """
        resources: list[dict[str, Any]] = []
        if {"coupon", "deal"} & set(deal_types):
            resources.append(
                {
                    "name": "vouchers",
                    "params": {"voucherType": "voucher"},
                    "map": self._map_voucher,
                }
            )
        if {"sale", "deal"} & set(deal_types):
            resources.append(
                {
                    "name": "deals",
                    "params": {"dealType": "deal"},
                    "map": self._map_deal,
                }
            )
        return resources

    @staticmethod
    def _endpoint(resource: dict[str, Any], publisher_id: Any, advertiser_id: Any) -> str:
        base = f"/publishers/{publisher_id}"
        if advertiser_id:
            base += f"/advertisers/{advertiser_id}"
        return f"{base}/{resource['name']}"

    async def fetch(
        self,
        config: dict[str, Any],
        *,
        mode: "SyncMode | None" = None,
        credentials: SourceCredentials | None = None,
    ) -> list[RawOffer]:
        """Non-streaming convenience wrapper. Prefer `stream` in the pipeline.

        Kept because a `fetch`-only adapter is the common case and callers should
        not have to care; but it inherits the limitation that a mid-pagination
        failure raises rather than returning a truncated list, so anything that
        cares about partial results must use `stream`.
        """
        collected: list[RawOffer] = []
        async for offer, _pages in self.stream(
            config, mode=mode, credentials=credentials
        ):
            collected.append(offer)
        return collected

    def _map_voucher(self, item: dict[str, Any]) -> RawOffer | None:
        """Map Awin voucher object to RawOffer."""
        try:
            # Awin voucher fields: title, description, voucherCode, startDate, endDate,
            # advertiserName, advertiserId, deepLink, discountAmount, discountPercentage,
            # discountType, currency, categories, regions
            title = item.get("title") or item.get("description") or "Awin Voucher"
            code = item.get("voucherCode")
            if not code:
                return None  # skip vouchers without codes

            destination = item.get("deepLink") or item.get("trackingUrl")
            if not destination:
                return None

            discount_type = item.get("discountType")
            discount_value = item.get("discountAmount") or item.get("discountPercentage")
            discount_text = None
            if discount_type == "percentage" and discount_value:
                discount_text = f"{discount_value}% off"
            elif discount_type == "fixed" and discount_value:
                discount_text = f"{discount_value} off"

            return RawOffer(
                title=title,
                code=code.strip().upper(),
                description=item.get("description"),
                discount_type=discount_type,
                discount_value=float(discount_value) if discount_value else None,
                discount_text=discount_text,
                destination_url=destination,
                expires_at=item.get("endDate"),
                external_id=str(item.get("id")) if item.get("id") else None,
                store_slug=item.get("advertiserName", "").lower().replace(" ", "-"),
                store_name=item.get("advertiserName"),
                category_slug=None,  # Awin categories don't map directly
                store_website=item.get("advertiserUrl"),
            )
        except (KeyError, ValueError, TypeError):
            # Log and skip bad items rather than failing the whole fetch
            return None

    def _map_deal(self, item: dict[str, Any]) -> RawOffer | None:
        """Map Awin deal object to RawOffer."""
        try:
            title = item.get("title") or item.get("description") or "Awin Deal"
            destination = item.get("deepLink") or item.get("trackingUrl")
            if not destination:
                return None

            discount_type = item.get("dealType")  # "sale", "offer", etc.
            discount_value = item.get("discountAmount") or item.get("discountPercentage")
            discount_text = None
            if discount_type == "percentage" and discount_value:
                discount_text = f"{discount_value}% off"
            elif discount_type == "fixed" and discount_value:
                discount_text = f"{discount_value} off"

            return RawOffer(
                title=title,
                code=None,  # deals don't have codes
                description=item.get("description"),
                discount_type=discount_type,
                discount_value=float(discount_value) if discount_value else None,
                discount_text=discount_text,
                destination_url=destination,
                expires_at=item.get("endDate"),
                external_id=str(item.get("id")) if item.get("id") else None,
                store_slug=item.get("advertiserName", "").lower().replace(" ", "-"),
                store_name=item.get("advertiserName"),
                category_slug=None,
                store_website=item.get("advertiserUrl"),
            )
        except (KeyError, ValueError, TypeError):
            return None
