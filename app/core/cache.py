"""Small Redis-backed cache helpers for hot read paths.

Everything here fails open: a Redis error or timeout never propagates to the
request. The cache is an optimization, not a dependency.
"""

import hashlib
import json
from typing import Any

from redis.asyncio import Redis

from app.core.config import get_settings

_redis: Redis | None = None


def get_redis() -> Redis | None:
    global _redis
    settings = get_settings()
    if not settings.redis_url:
        return None
    if _redis is None:
        _redis = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=0.5,
            socket_connect_timeout=0.5,
            health_check_interval=30,
        )
    return _redis


async def cache_get(key: str) -> Any | None:
    r = get_redis()
    if r is None:
        return None
    try:
        raw = await r.get(key)
    except Exception:  # noqa: BLE001
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


async def cache_set(key: str, value: Any, ttl: int = 45) -> None:
    r = get_redis()
    if r is None:
        return
    try:
        await r.set(key, json.dumps(value, default=str), ex=ttl)
    except Exception:  # noqa: BLE001
        return


async def coupon_cache_version() -> str:
    r = get_redis()
    if r is None:
        return "0"
    try:
        val = await r.get("coupons:ver")
        if val is None:
            return "0"
        return str(val)
    except Exception:  # noqa: BLE001
        return "0"


async def bump_coupon_cache_version() -> None:
    r = get_redis()
    if r is None:
        return
    try:
        await r.incr("coupons:ver")
    except Exception:  # noqa: BLE001
        return


def normalized_params_hash(params: dict[str, Any]) -> str:
    payload = json.dumps(params, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode()).hexdigest()
