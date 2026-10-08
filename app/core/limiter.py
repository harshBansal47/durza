"""Shared rate limiter (slowapi / the `limits` library underneath).

Rate limiting is one of the few things that *must* be shared across instances to
mean anything. With N replicas each holding its own counters, a client gets N
times the limit by spreading requests over them, so the limit is not a limit at
all — and because the abuse it exists to stop still works, nothing fails
visibly.

So `REDIS_URL` is honoured when it is set. It is not set by default: in-memory
storage is correct for a single instance and for CI, and defaulting it to a
Redis URI would mean a fresh checkout fails to start unless a Redis happens to
be running.

    REDIS_URL unset    -> per-process counters, correct for one replica
    REDIS_URL=redis:// -> shared counters, required for more than one

The consequence of leaving it unset in production is worth stating plainly: with
several replicas behind a load balancer the effective limit is multiplied by the
replica count. `/health/deep` does not check this, because "this deployment is
configured correctly" is not a property of the database, the scheduler or the
mail server — it is a property of the environment, and the only place it can be
asserted is the environment itself.
"""

from slowapi import Limiter

from app.core.config import get_settings
from app.core.request_meta import client_ip

_settings = get_settings()

# `storage_uri=None` is slowapi's own "in-memory" spelling, not a missing
# argument: passing nothing would leave the default in place anyway, and passing
# an empty string would be a parse error rather than a fallback.
limiter = Limiter(key_func=client_ip, storage_uri=_settings.redis_url)
