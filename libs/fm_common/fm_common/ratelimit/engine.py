"""Redis Sliding-Window Rate-Limiting Engine.

Each identity gets a sorted set whose members are request ids scored by arrival time (ms).
Per request, atomically (MULTI/EXEC):

    ZREMRANGEBYSCORE key 0 (now - window)   # evict requests that slid out of the window
    ZADD             key now <unique-member>  # record this request
    ZCARD            key                       # how many in the current window
    PEXPIRE          key window                # idle keys clean themselves up

Unlike fixed windows there is no 2x burst at window boundaries. Rejected requests are
also recorded, so a client hammering the API stays blocked until it actually backs off.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_after_ms: int  # when the oldest request in the window expires


class SlidingWindowLimiter:
    def __init__(self, redis: Redis, prefix: str = "fm:rl:") -> None:
        self.redis = redis
        self.prefix = prefix

    async def hit(self, identity: str, limit: int, window_seconds: int) -> RateLimitResult:
        key = f"{self.prefix}{identity}"
        now_ms = int(time.time() * 1000)
        window_ms = window_seconds * 1000
        member = f"{now_ms}:{uuid.uuid4().hex[:8]}"

        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(key, 0, now_ms - window_ms)
            pipe.zadd(key, {member: now_ms})
            pipe.zcard(key)
            pipe.zrange(key, 0, 0, withscores=True)
            pipe.pexpire(key, window_ms)
            _, _, count, oldest, _ = await pipe.execute()

        oldest_ms = int(oldest[0][1]) if oldest else now_ms
        reset_after = max(0, oldest_ms + window_ms - now_ms)
        return RateLimitResult(
            allowed=count <= limit,
            limit=limit,
            remaining=max(0, limit - count),
            reset_after_ms=reset_after,
        )
