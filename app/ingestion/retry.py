import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

logger = logging.getLogger("durza.ingestion")


async def with_retries[T](
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    retryable: tuple[type[Exception], ...] = (Exception,),
) -> T:
    """Retry an async call with exponential backoff + jitter. Last error re-raised."""
    for attempt in range(attempts):
        try:
            return await fn()
        except retryable as exc:
            if attempt == attempts - 1:
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            logger.warning(
                "call failed, retrying",
                extra={
                    "attempt": attempt + 1,
                    "attempts": attempts,
                    "error": type(exc).__name__,
                    "retry_in_s": round(delay, 2),
                },
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
