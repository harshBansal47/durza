"""Admitad Publisher API coupon adapter."""

from collections.abc import AsyncIterator
from functools import partial
from typing import TYPE_CHECKING, Any

import httpx

from app.ingestion.base import PartialFetchError, RawOffer
from app.ingestion.credentials import SourceCredentials
from app.ingestion.retry import with_retries
from app.integrations.admitad import get_access_token, page_results

if TYPE_CHECKING:
    from app.models.ingestion_run import SyncMode

_BASE_URL = "https://api.admitad.com"
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_USER_AGENT = "DurzaBot/1.0 (+https://durza.in/bot)"


class AdmitadSource:
    """Fetch coupons available to one Admitad publisher website."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def stream(
        self,
        config: dict[str, Any],
        *,
        mode: "SyncMode | None" = None,
        credentials: SourceCredentials | None = None,
    ) -> AsyncIterator[tuple[RawOffer, int]]:
        if credentials is None:
            raise ValueError("admitad source requires client ID and client secret credentials")
        client_id = credentials.require("ADMITAD_CLIENT_ID")
        client_secret = credentials.require("ADMITAD_CLIENT_SECRET")
        website_id = config.get("website_id")
        if not website_id:
            raise ValueError("admitad source config requires 'website_id'")
        try:
            page_size = int(config.get("page_size", 100))
        except (TypeError, ValueError) as exc:
            raise ValueError("admitad source 'page_size' must be an integer from 1 to 500") from exc
        if not 1 <= page_size <= 500:
            raise ValueError("admitad source 'page_size' must be an integer from 1 to 500")

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=_TIMEOUT)
        pages = 0
        failure: Exception | None = None
        try:
            token = await get_access_token(client, client_id=client_id, client_secret=client_secret)
            headers = {
                "User-Agent": _USER_AGENT,
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
            offset = 0
            while True:

                async def _request(offset: int = offset) -> tuple[list[dict[str, Any]], bool]:
                    response = await client.get(
                        f"{_BASE_URL}/coupons/website/{website_id}/",
                        params={"limit": page_size, "offset": offset},
                        headers=headers,
                    )
                    response.raise_for_status()
                    return page_results(
                        response.json(),
                        offset=offset,
                        limit=page_size,
                        resource="coupons",
                    )

                try:
                    rows, has_next = await with_retries(
                        partial(_request), retryable=(httpx.HTTPError, TypeError)
                    )
                except (httpx.HTTPError, TypeError) as exc:
                    failure = exc
                    break

                if not rows:
                    break
                pages += 1
                for row in rows:
                    offer = self._map_coupon(row)
                    if offer is not None:
                        yield offer, pages
                offset += len(rows)
                if not has_next:
                    break
        finally:
            if owns_client:
                await client.aclose()

        if failure is not None:
            raise PartialFetchError(
                f"admitad pagination stopped after page {pages}: {type(failure).__name__}",
                pages_completed=pages,
            ) from failure

    async def fetch(
        self,
        config: dict[str, Any],
        *,
        mode: "SyncMode | None" = None,
        credentials: SourceCredentials | None = None,
    ) -> list[RawOffer]:
        offers: list[RawOffer] = []
        async for offer, _pages in self.stream(config, mode=mode, credentials=credentials):
            offers.append(offer)
        return offers

    @staticmethod
    def _map_coupon(item: dict[str, Any]) -> RawOffer | None:
        campaign = item.get("campaign")
        campaign = campaign if isinstance(campaign, dict) else {}
        title = item.get("name") or item.get("title") or item.get("description")
        destination = (
            item.get("goto_link")
            or item.get("tracking_url")
            or item.get("tracking_link")
            or item.get("click_url")
        )
        if not title or not destination:
            return None

        code = item.get("promo_code") or item.get("promocode") or item.get("code")
        discount_type = item.get("discount_type") or item.get("discountType")
        discount_value = item.get("discount_value") or item.get("discount_amount")
        discount = item.get("discount")
        if discount_value is None and isinstance(discount, (int, float)):
            discount_value = discount
        if not discount_type and isinstance(discount, str) and "%" in discount:
            discount_type = "percentage"
            try:
                discount_value = float(discount.replace("%", "").strip())
            except ValueError:
                discount_value = None
        try:
            discount_value = float(discount_value) if discount_value is not None else None
        except (TypeError, ValueError):
            discount_value = None

        store_name = (
            campaign.get("name") or item.get("campaign_name") or item.get("advertiser_name")
        )
        store_url = campaign.get("site_url") or campaign.get("url") or item.get("advertiser_url")
        return RawOffer(
            title=str(title),
            code=str(code).strip().upper() if code else None,
            description=item.get("description"),
            discount_type=str(discount_type) if discount_type else None,
            discount_value=discount_value,
            discount_text=str(discount) if discount else None,
            destination_url=str(destination),
            expires_at=item.get("date_end") or item.get("end_date") or item.get("endDate"),
            external_id=str(item.get("id") or item.get("coupon_id") or "") or None,
            store_slug=str(store_name).lower().replace(" ", "-") if store_name else None,
            store_name=str(store_name) if store_name else None,
            store_website=str(store_url) if store_url else None,
        )
