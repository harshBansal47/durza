from typing import TYPE_CHECKING, Any

import httpx

from app.ingestion.base import RawOffer
from app.ingestion.normalize import offer_from_dict
from app.ingestion.retry import with_retries

if TYPE_CHECKING:
    from app.ingestion.credentials import SourceCredentials
    from app.models.ingestion_run import SyncMode

_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_USER_AGENT = "DurzaBot/1.0 (+https://durza.in/bot)"


class FeedSource:
    """Fetches offers from a JSON URL.

    `config = {"url": "https://…/offers.json"}` yields either a JSON list of offer
    objects or `{"offers": [...]}`.

    The publisher's token comes from `credentials`, not from `config`. It used to
    be read out of an optional `config["headers"]` dict, which meant the API key
    sat in a JSONB column in the database — visible in the admin panel, in every
    backup, and in any query that dumps a source row. Reading it from the
    environment instead is the whole reason `credential_ref` exists, so the header
    dict is gone rather than kept as a second, worse way in.

    The credential is optional: most feeds are public, and `feed` is deliberately
    not in `credentials.CREDENTIAL_KINDS`. A feed that does set `credential_ref`
    sends it as a bearer token, and health still requires it to resolve.
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def fetch(
        self,
        config: dict[str, Any],
        *,
        mode: "SyncMode | None" = None,
        credentials: "SourceCredentials | None" = None,
    ) -> list[RawOffer]:
        # `mode` is ignored. A single JSON document is one payload; narrowing it
        # would have to be done by the publisher, not by us.
        url = config.get("url")
        if not url:
            raise ValueError("feed source config requires a 'url'")
        headers = {"User-Agent": _USER_AGENT}
        if credentials is not None and credentials.values:
            headers["Authorization"] = f"Bearer {credentials.only_value()}"
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=_TIMEOUT)

        # Per request, not baked into the client's defaults: with a client
        # injected (how every test runs) client-level headers are whatever the
        # caller configured, so an auth header attached there never arrives.
        async def _get() -> httpx.Response:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response

        try:
            response = await with_retries(_get, retryable=(httpx.HTTPError,))
        finally:
            if owns_client:
                await client.aclose()

        payload = response.json()
        items = payload.get("offers", payload) if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise TypeError("feed JSON must be a list or contain an 'offers' list")
        return [offer_from_dict(item) for item in items]
