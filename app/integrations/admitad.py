"""Shared OAuth helpers for the Admitad Publisher API."""

from typing import Any

import httpx

from app.ingestion.retry import with_retries

_TOKEN_URL = "https://api.admitad.com/token/"
_SCOPE = "advcampaigns websites coupons statistics deeplink_generator"


def page_results(
    payload: Any, *, offset: int, limit: int, resource: str
) -> tuple[list[dict[str, Any]], bool]:
    rows: Any
    if isinstance(payload, list):
        rows = payload
        has_next = False
        total = None
    elif isinstance(payload, dict):
        rows = payload.get("results")
        links = payload.get("_links")
        links = links if isinstance(links, dict) else {}
        metadata = payload.get("_meta")
        metadata = metadata if isinstance(metadata, dict) else {}
        has_next = bool(payload.get("next") or links.get("next"))
        total = metadata.get("count", payload.get("count"))
    else:
        rows = None
        has_next = False
        total = None

    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise TypeError(f"Admitad {resource} response has no results list")

    try:
        has_total = total is not None and offset + len(rows) < int(total)
    except (TypeError, ValueError):
        has_total = False
    has_next = has_next or has_total or len(rows) >= limit
    return rows, has_next


async def get_access_token(client: httpx.AsyncClient, *, client_id: str, client_secret: str) -> str:
    response = await with_retries(
        lambda: client.post(
            _TOKEN_URL,
            auth=(client_id, client_secret),
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "scope": _SCOPE,
            },
            headers={"Accept": "application/json"},
        )
    )
    response.raise_for_status()
    payload: Any = response.json()
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise TypeError("Admitad token response has no access_token")
    return token
