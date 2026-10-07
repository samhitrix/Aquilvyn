"""Arq worker for market-svc: live-price poller, history warm-up, EOD history refresh + corporate-action sync (E33b)."""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import select

from fm_common.config import settings
from fm_common.db.session import SessionLocal
from fm_common.events import publish
from fm_common.logging import configure_logging, get_logger
from fm_common.loopwatch import watch

from . import data
from .models import Instrument
from .providers import get_provider

log = get_logger("market.worker")


async def refresh_eod(ctx: dict[str, Any]) -> dict[str, int]:
    """After market close: pull the latest bars for every active traded instrument."""
    updated = 0
    async with SessionLocal() as db:
        insts = (await db.execute(select(Instrument).where(Instrument.is_active.is_(True)))).scalars().all()
    for inst in insts:
        if inst.asset_type.is_accrual:
            continue
        async with SessionLocal() as db:
            try:
                bars = await get_provider().history(inst.symbol, 10)
                updated += await data.upsert_bars(db, inst.id, [b.to_dict() for b in bars], get_provider().name)
            except Exception as exc:
                log.warning("eod.failed", symbol=inst.symbol, error=str(exc))
    await publish("price.eod_closed", {"instruments": len(insts)})
    log.info("eod.done", rows=updated)
    return {"rows": updated}


async def sync_corporate_actions(ctx: dict[str, Any]) -> dict[str, int]:
    new = 0
    async with SessionLocal() as db:
        insts = (await db.execute(select(Instrument).where(Instrument.asset_type == "stock", Instrument.is_active.is_(True)))).scalars().all()
        for inst in insts:
            try:
                new += await data.sync_corporate_actions(db, inst)
            except Exception as exc:
                log.warning("corp_actions.failed", symbol=inst.symbol, error=str(exc))
    return {"new": new}


async def refresh_fund_portfolios(ctx: dict[str, Any]) -> dict[str, int]:
    """Daily: fetch fund houses' monthly portfolio disclosures for every fund / ETF held (only what's due)."""
    from . import fund_holdings

    async with SessionLocal() as db:
        report = await fund_holdings.refresh(db)
    ok = sum(1 for r in report if r.get("ok") and not r.get("skipped"))
    failed = sum(1 for r in report if r.get("ok") is False)
    log.info("funds.refreshed", fund_houses=len(report), fetched=ok, failed=failed)
    return {"fund_houses": len(report), "fetched": ok, "failed": failed}


async def heartbeat(ctx: dict[str, Any]) -> None:
    log.info("worker.heartbeat")


async def startup(ctx: dict[str, Any]) -> None:
    await watch.start("market-worker")  # `fm.py perf` shows what this worker is busy with
    settings.service_name = "market-worker"
    configure_logging(settings.log_level, service="market-worker")
    from .api import warm_history
    from .stream import poller

    # long-running background work lives here, not in the API process
    ctx["background"] = [asyncio.create_task(poller(), name="price-poller"), asyncio.create_task(warm_history(), name="history-warmup")]


async def shutdown(ctx: dict[str, Any]) -> None:
    for t in ctx.get("background", []):
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    queue_name = "fm:q:market"
    functions = [refresh_eod, sync_corporate_actions, refresh_fund_portfolios]
    on_startup = startup
    on_shutdown = shutdown
    # 16:15 IST = 10:45 UTC, weekdays; corporate actions at 18:30 IST
    cron_jobs = [
        cron(refresh_eod, weekday={0, 1, 2, 3, 4}, hour=10, minute=45),
        cron(sync_corporate_actions, hour=13, minute=0),
        cron(refresh_fund_portfolios, hour=3, minute=30, run_at_startup=True),  # 09:00 IST; fund houses publish by ~the 10th
        cron(heartbeat, minute={0, 15, 30, 45}),
    ]
