"""Live check that a destination URL still resolves. Off by default: probing
every offer is slow and some sites block bots, so sources opt in via
`probe_destinations`. Provider supplied URLs are untrusted, so private network
targets and redirects are rejected to keep probing from becoming an SSRF path."""

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

import httpx

from app.ingestion.retry import with_retries

PROBE_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_USER_AGENT = "DurzaBot/1.0 (+https://durza.in/bot)"


async def probe_destination(url: str) -> tuple[bool, str | None]:
    """Return (ok, failure_reason). ok=True means the URL gave a 2xx/3xx.

    Transient errors (timeouts, connection errors, 5xx) are retried up to 3
    times with backoff before the coupon is judged failed.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False, "destination probe requires an HTTP(S) URL"
        try:
            addresses = [ipaddress.ip_address(parsed.hostname)]
        except ValueError:
            try:
                records = await asyncio.get_running_loop().getaddrinfo(
                    parsed.hostname,
                    parsed.port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            except OSError:
                return False, "destination hostname did not resolve"
            addresses = [ipaddress.ip_address(record[4][0]) for record in records]
        if not addresses or any(not address.is_global for address in addresses):
            return False, "destination resolves to a non-public address"

        async with httpx.AsyncClient(
            timeout=PROBE_TIMEOUT, follow_redirects=False, headers={"User-Agent": _USER_AGENT}
        ) as client:

            async def _get() -> httpx.Response:
                response = await client.get(url)
                if response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        "server error", request=response.request, response=response
                    )
                return response

            try:
                response = await with_retries(_get, retryable=(httpx.HTTPError,))
            except httpx.TimeoutException:
                return False, "destination probe timed out"
            except httpx.HTTPError as exc:
                return False, f"destination probe failed: {type(exc).__name__}"
            # 3xx confirms the destination is reachable. Do not follow it:
            # redirect targets are another untrusted URL and can reach private
            # services even when the initial host is public.
            if response.status_code < 400:
                return True, None
            return False, f"destination returned HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        return False, f"destination probe failed: {type(exc).__name__}"
