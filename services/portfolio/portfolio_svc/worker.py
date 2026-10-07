"""Arq worker for portfolio-svc: daily portfolio snapshots (performance chart)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from fm_common.config import settings
from fm_common.db.session import SessionLocal
from fm_common.deps import principal_from_token
from fm_common.identity.tokens import create_service_token
from fm_common.logging import configure_logging, get_logger
from fm_common.loopwatch import watch

from . import holdings, imports
from .models import Portfolio, PortfolioSnapshot, Profile, Transaction

log = get_logger("portfolio.worker")


async def snapshot_all(ctx: dict[str, Any]) -> dict[str, int]:
    n = 0
    async with SessionLocal() as db:
        hids = list((await db.execute(select(Transaction.household_id).where(Transaction.deleted_at.is_(None)).distinct())).scalars())
    for hid in hids:
        principal = await principal_from_token(create_service_token("portfolio-worker", hid, ttl=600))
        async with SessionLocal() as db:
            pfs = list((await db.execute(select(Portfolio).where(Portfolio.household_id == hid, Portfolio.deleted_at.is_(None)))).scalars())
            profiles = {p.id: p for p in (await db.execute(select(Profile).where(Profile.household_id == hid))).scalars()}
            for pf in pfs:
                res = await holdings.compute(db, principal, [pf], profiles)
                s = res["summary"]
                if not res["holdings"]:
                    continue
                stmt = insert(PortfolioSnapshot).values(
                    portfolio_id=pf.id, snapshot_date=date.today(), household_id=hid, invested=Decimal(str(s["invested"])),
                    market_value=Decimal(str(s["market_value"])), is_complete=not s["unpriced"],
                )
                await db.execute(stmt.on_conflict_do_update(index_elements=["portfolio_id", "snapshot_date"],
                                                            set_={"invested": stmt.excluded.invested, "market_value": stmt.excluded.market_value}))
                n += 1
            await db.commit()
    log.info("snapshots.done", portfolios=n)
    return {"snapshots": n}


async def settle_fund_sources_all(ctx: dict[str, Any]) -> dict[str, int]:
    """A fund in both a CAS and a broker holdings statement counts once (the CAS) — also for data imported earlier."""
    n = 0
    async with SessionLocal() as db:
        hids = list((await db.execute(select(Transaction.household_id).where(Transaction.deleted_at.is_(None)).distinct())).scalars())
    for hid in hids:
        try:
            principal = await principal_from_token(create_service_token("portfolio-worker", hid, ttl=600))
            async with SessionLocal() as db:
                n += await imports.settle_fund_sources(db, principal)
        except Exception as exc:  # one household's problem must not stop the others
            log.warning("fund_sources.settle_failed", household_id=str(hid), error=str(exc)[:200])
    return {"rows_set_aside": n}


async def startup(ctx: dict[str, Any]) -> None:
    await watch.start("portfolio-worker")  # `fm.py perf` shows what this worker is busy with
    settings.service_name = "portfolio-worker"
    configure_logging(settings.log_level, service="portfolio-worker")


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    queue_name = "fm:q:portfolio"
    functions = [snapshot_all, settle_fund_sources_all]
    on_startup = startup
    cron_jobs = [cron(snapshot_all, hour=11, minute=0),  # 16:30 IST, after EOD refresh
                 cron(settle_fund_sources_all, minute=23, run_at_startup=True)]  # hourly + on start: fixes earlier double imports


