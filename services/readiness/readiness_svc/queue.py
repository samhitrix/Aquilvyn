from __future__ import annotations

import time
from typing import Any

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from fm_common.config import settings

QUEUE = "fm:q:readiness"
_pool: ArqRedis | None = None


async def pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(settings.redis_url), default_queue_name=QUEUE)
    return _pool


async def enqueue_check(household_id: str, trigger: str, defer_by: float | None = None) -> bool:
    """One pending check per household per minute — bursts of events collapse into a single pass."""
    p = await pool()
    job = f"check:{household_id}:{int(time.time() // 60)}"
    return await p.enqueue_job("check_household_job", household_id, trigger, _job_id=job, _defer_by=defer_by, _queue_name=QUEUE) is not None


async def enqueue(fn: str, *args: Any, job_id: str | None = None) -> None:
    p = await pool()
    await p.enqueue_job(fn, *args, _job_id=job_id, _queue_name=QUEUE)


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None
