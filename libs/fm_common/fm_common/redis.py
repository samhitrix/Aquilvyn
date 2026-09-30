from __future__ import annotations

from redis.asyncio import Redis

from fm_common.config import settings

_redis: Redis | None = None


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(settings.redis_url, decode_responses=False, health_check_interval=30)
    return _redis


def set_redis(client: Redis) -> None:
    """Test hook (fakeredis)."""
    global _redis
    _redis = client


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
