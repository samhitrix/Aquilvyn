"""Arq worker for advisor-svc: debounced event re-runs, nightly full runs, multi-AI reviews +
narration, and the daily scorecard evaluation."""
from __future__ import annotations

import uuid
from typing import Any

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import select

from fm_common import http
from fm_common.config import settings as common
from fm_common.db.session import SessionLocal
from fm_common.events import publish
from fm_common.identity.tokens import create_service_token
from fm_common.logging import configure_logging, get_logger
from fm_common.loopwatch import watch

from . import engine, shadow
from .ai import narrate, review
from .models import Recommendation
from .queue import QUEUE, enqueue

log = get_logger("advisor.worker")


async def run_household_job(ctx: dict[str, Any], household_id: str, trigger: str = "nightly", profile_id: str | None = None,
                            instrument_ids: list[str] | None = None) -> dict[str, Any]:
    hid = uuid.UUID(household_id)
    token = create_service_token("advisor-worker", hid, ttl=900)
    try:
        async with SessionLocal() as db:
            result = await engine.run_household(db, hid, token, trigger, uuid.UUID(profile_id) if profile_id else None,
                                                instrument_ids=[uuid.UUID(i) for i in instrument_ids] if instrument_ids else None)
    except Exception as exc:
        await engine.mark_failed(hid, exc)
        raise
    if result["to_review"]:
        await enqueue("review_and_narrate", household_id, result["to_review"], job_id=f"review:{result['run_id']}")
    return {k: v for k, v in result.items() if k != "health"}


async def review_and_narrate(ctx: dict[str, Any], household_id: str, rec_ids: list[str], force: bool = False) -> dict[str, Any]:
    hid = uuid.UUID(household_id)
    ids = [uuid.UUID(x) for x in rec_ids]
    async with SessionLocal() as db:
        res = await review.review_recommendations(db, hid, ids, force=force)
        recs = list((await db.execute(select(Recommendation).where(Recommendation.id.in_(ids)))).scalars())
        await narrate.narrate(db, hid, recs[:15])
    await publish("advisor.reviews_done", {"reviewed": res.get("reviewed", 0), "mode": res.get("mode")}, household_id=household_id)
    return res


async def nightly_all(ctx: dict[str, Any]) -> int:
    hids = await http.get(common.portfolio_url, "/api/v1/internal/households", token=create_service_token("advisor-worker"))
    for hid in hids:
        await enqueue("run_household_job", hid, "nightly", job_id=f"nightly:{hid}")
    return len(hids)


async def evaluate_scorecard(ctx: dict[str, Any]) -> int:
    token = create_service_token("advisor-worker")
    async with SessionLocal() as db:
        syms = sorted({s for s in (await db.execute(select(Recommendation.symbol).where(Recommendation.symbol.isnot(None)).distinct())).scalars()})
        if not syms:
            return 0
        quotes = await http.get(common.market_url, "/api/v1/market/quotes", token=token, params={"symbols": ",".join(syms + ["^NSEI"])})
        return await shadow.evaluate_outcomes(db, quotes, (quotes.get("^NSEI") or {}).get("price"))


async def startup(ctx: dict[str, Any]) -> None:
    await watch.start("advisor-worker")  # `fm.py perf` shows what this worker is busy with
    common.service_name = "advisor-worker"
    configure_logging(common.log_level, service="advisor-worker")


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(common.redis_url)
    queue_name = QUEUE
    functions = [run_household_job, review_and_narrate, nightly_all, evaluate_scorecard]
    on_startup = startup
    max_jobs = 4
    job_timeout = 900
    # 17:00 IST (11:30 UTC) after EOD + snapshots; scorecard at 18:00 IST
    cron_jobs = [cron(nightly_all, hour=11, minute=30), cron(evaluate_scorecard, hour=12, minute=30)]
