"""Arq worker of the readiness engine: a pass per household every 15 minutes, plus event-driven passes."""
from __future__ import annotations

import uuid
from typing import Any

from arq import Retry, cron
from arq.connections import RedisSettings

from fm_common import http
from fm_common.config import settings as common
from fm_common.db.session import SessionLocal
from fm_common.identity.tokens import create_service_token
from fm_common.logging import configure_logging, get_logger
from fm_common.loopwatch import watch

from . import engine
from .config import settings
from .queue import QUEUE, enqueue_check

log = get_logger("readiness.worker")


async def check_household_job(ctx: dict[str, Any], household_id: str, trigger: str = "scheduled") -> dict[str, Any]:
    async with SessionLocal() as db:
        return await engine.check_household(db, uuid.UUID(household_id), trigger)


async def sweep_all(ctx: dict[str, Any]) -> int:
    try:
        hids = await http.get(common.portfolio_url, "/api/v1/internal/households", token=create_service_token("readiness-worker"))
    except Exception as exc:  # e.g. at start-up, before the portfolio service answers: try again shortly
        if ctx.get("job_try", 1) < 6:
            log.warning("readiness.sweep_deferred", error=str(exc)[:200])
            raise Retry(defer=30) from exc
        raise
    for hid in hids:
        await enqueue_check(hid, "scheduled")
    return len(hids)


async def startup(ctx: dict[str, Any]) -> None:
    await watch.start("readiness-worker")  # `fm.py perf` shows what this worker is busy with
    common.service_name = "readiness-worker"
    configure_logging(common.log_level, service="readiness-worker")


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(common.redis_url)
    queue_name = QUEUE
    functions = [check_household_job, sweep_all]
    on_startup = startup
    max_jobs = 2
    job_timeout = 900
    cron_jobs = [cron(sweep_all, minute=set(range(0, 60, settings.sweep_every_minutes)), run_at_startup=True)]
