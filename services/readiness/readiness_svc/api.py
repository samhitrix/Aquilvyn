from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from fm_common.deps import DB, Principal, require
from fm_common.identity.rbac import P_ADVISOR_READ, P_ADVISOR_RUN

from . import checks as ck
from .config import settings
from .models import HoldingCheck, Sweep
from .queue import enqueue_check

router = APIRouter(prefix="/readiness", tags=["readiness"])


def row_out(r: HoldingCheck) -> dict[str, Any]:
    return {"rec_id": str(r.rec_id) if r.rec_id else None, "profile_id": str(r.profile_id) if r.profile_id else None,
            "profile_name": r.profile_name, "instrument_id": str(r.instrument_id), "symbol": r.symbol, "name": r.name,
            "asset_type": r.asset_type, "status": r.status, "checks": r.checks, "fixes": r.fixes, "checked_at": r.checked_at}


@router.get("")
async def readiness(db: DB, principal: Principal = require(P_ADVISOR_READ), profile_id: uuid.UUID | None = None) -> dict[str, Any]:
    """Every holding's checks (fundamentals · technicals · analysis up to date · AI review), what the engine is doing
    about the failing ones, and when it last / next looks."""
    q = select(HoldingCheck).where(HoldingCheck.household_id == principal.household_id)
    if profile_id:
        q = q.where(HoldingCheck.profile_id == profile_id)
    rank = {"attention": 0, "fixing": 1, "ready": 2}  # problems first
    rows = sorted((await db.execute(q)).scalars().all(), key=lambda r: (rank.get(r.status, 3), r.symbol))
    last = await db.scalar(select(Sweep).where(Sweep.household_id == principal.household_id)
                           .order_by(Sweep.started_at.desc()).limit(1))
    summary = {"holdings": len(rows), "ready": 0, "fixing": 0, "attention": 0, "failing": dict.fromkeys(ck.CHECKS, 0)}
    for r in rows:
        summary[r.status] = summary.get(r.status, 0) + 1
        for c in ck.CHECKS:
            summary["failing"][c] += (r.checks.get(c) or {}).get("state") in ("failed", "waiting")
    now = datetime.now(UTC)
    step = settings.sweep_every_minutes
    nxt = now.replace(second=0, microsecond=0) + timedelta(minutes=step - now.minute % step)
    return {
        "summary": summary, "labels": ck.LABEL, "holdings": [row_out(r) for r in rows],
        "last_pass": {"started_at": last.started_at, "finished_at": last.finished_at, "status": last.status, "trigger": last.trigger,
                      "error": last.error, "stats": last.stats} if last else None,
        "next_pass": nxt.isoformat(), "every_minutes": step,
    }


@router.post("/check")
async def check_now(principal: Principal = require(P_ADVISOR_RUN)) -> dict[str, Any]:
    """Run a pass now (in the background) — the page refreshes as it finishes."""
    queued = await enqueue_check(str(principal.household_id), "manual")
    return {"queued": queued}
