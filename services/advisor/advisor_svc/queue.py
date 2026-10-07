from __future__ import annotations

from typing import Any

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from fm_common.config import settings

QUEUE = "fm:q:advisor"
_pool: ArqRedis | None = None


async def pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(settings.redis_url), default_queue_name=QUEUE)
    return _pool


async def enqueue(fn: str, *args: Any, job_id: str | None = None, defer_by: float | None = None) -> None:
    p = await pool()
    await p.enqueue_job(fn, *args, _job_id=job_id, _defer_by=defer_by, _queue_name=QUEUE)


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None
