"""Live check that a destination URL still resolves. Off by default: probing
every offer is slow and some sites block bots, so sources opt in via
`probe_destinations`."""

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
        async with httpx.AsyncClient(
            timeout=PROBE_TIMEOUT, follow_redirects=True, headers={"User-Agent": _USER_AGENT}
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
            if response.status_code < 400:
                return True, None
            return False, f"destination returned HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        return False, f"destination probe failed: {type(exc).__name__}"
