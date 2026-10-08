import asyncio
import random
from collections.abc import Awaitable, Callable


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
        except retryable:
            if attempt == attempts - 1:
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
