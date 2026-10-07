from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import orjson
from redis.asyncio import Redis

from fm_common.config import settings
from fm_common.logging import get_logger
from fm_common.redis import get_redis

from .l1 import MISSING, L1Cache

log = get_logger(__name__)
T = TypeVar("T")
INVALIDATION_CHANNEL = "cache:invalidate"
NAMESPACE = "fm:c:"


def _dumps(v: Any) -> bytes:
    return orjson.dumps(v, default=str, option=orjson.OPT_SERIALIZE_NUMPY)


class CacheEngine:
    def __init__(self, redis_factory: Callable[[], Redis] = get_redis) -> None:
        self.l1 = L1Cache(settings.cache_l1_max_items, settings.cache_l1_ttl_seconds)
        self._redis_factory = redis_factory
        self._inflight: dict[str, asyncio.Future[Any]] = {}
        self._listener: asyncio.Task[None] | None = None

    @property
    def redis(self) -> Redis:
        return self._redis_factory()

    async def get(self, key: str) -> Any:
        v = self.l1.get(key)
        if v is not MISSING:
            return v
        try:
            raw = await self.redis.get(NAMESPACE + key)
        except Exception as exc:  # Redis down => degrade to loader, never fail the request
            log.warning("cache.l2_unavailable", error=str(exc))
            return MISSING
        if raw is None:
            return MISSING
        value = orjson.loads(raw)
        self.l1.set(key, value)
        return value

    async def set(self, key: str, value: Any, ttl: int | None = None, l1_ttl: float | None = None) -> None:
        self.l1.set(key, value, l1_ttl)
        try:
            await self.redis.set(NAMESPACE + key, _dumps(value), ex=ttl or settings.cache_l2_ttl_seconds)
        except Exception as exc:
            log.warning("cache.l2_set_failed", error=str(exc))

    async def get_or_load(
        self, key: str, loader: Callable[[], Awaitable[T]], ttl: int | None = None, l1_ttl: float | None = None
    ) -> T:
        value = await self.get(key)
        if value is not MISSING:
            return value  # type: ignore[no-any-return]
        # Single-flight: piggyback on an in-progress load for the same key.
        if (fut := self._inflight.get(key)) is not None:
            return await asyncio.shield(fut)  # type: ignore[no-any-return]
        fut = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            result = await loader()
            # Round-trip through JSON so L1 holds the same shape an L2 hit would return.
            result = orjson.loads(_dumps(result))
            await self.set(key, result, ttl, l1_ttl)
            fut.set_result(result)
            return result
        except BaseException as exc:
            fut.set_exception(exc)
            fut.exception()  # mark retrieved; waiters re-raise it themselves
            raise
        finally:
            self._inflight.pop(key, None)

    async def invalidate(self, *keys: str, prefix: str | None = None) -> None:
        for k in keys:
            self.l1.delete(k)
        if prefix:
            self.l1.delete_prefix(prefix)
        try:
            if keys:
                await self.redis.delete(*(NAMESPACE + k for k in keys))
            if prefix:
                async for k in self.redis.scan_iter(match=f"{NAMESPACE}{prefix}*", count=500):
                    await self.redis.delete(k)
            await self.redis.publish(INVALIDATION_CHANNEL, orjson.dumps({"keys": list(keys), "prefix": prefix}))
        except Exception as exc:
            log.warning("cache.invalidate_failed", error=str(exc))

    # ---- cross-replica L1 coherence ----
    async def start_listener(self) -> None:
        if self._listener is None:
            self._listener = asyncio.create_task(self._listen(), name="cache-invalidation-listener")

    async def stop_listener(self) -> None:
        if self._listener:
            self._listener.cancel()
            self._listener = None

    async def _listen(self) -> None:
        while True:
            try:
                pubsub = self.redis.pubsub()
                await pubsub.subscribe(INVALIDATION_CHANNEL)
                async for msg in pubsub.listen():
                    if msg.get("type") != "message":
                        continue
                    data = orjson.loads(msg["data"])
                    for k in data.get("keys", []):
                        self.l1.delete(k)
                    if data.get("prefix"):
                        self.l1.delete_prefix(data["prefix"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("cache.listener_error", error=str(exc))
                await asyncio.sleep(2)

    def stats(self) -> dict[str, int]:
        return {"l1_items": len(self.l1), "l1_hits": self.l1.hits, "l1_misses": self.l1.misses}


cache = CacheEngine()


def cached(key_fn: Callable[..., str], ttl: int | None = None, l1_ttl: float | None = None):
    """Decorator for async functions: ``@cached(lambda sym: f"quote:{sym}", ttl=5)``."""

    def deco(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            return await cache.get_or_load(key_fn(*args, **kwargs), lambda: fn(*args, **kwargs), ttl, l1_ttl)

        return wrapper

    return deco
