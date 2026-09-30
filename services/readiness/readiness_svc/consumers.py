"""Event-driven passes: whenever something that can change a holding's readiness happens, check that household
again (debounced) — an analysis finished, AI reviews landed, a statement was imported, a stock's data arrived."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from fm_common.db.session import SessionLocal
from fm_common.events import EventConsumer

from .config import settings
from .models import HoldingCheck
from .queue import enqueue_check


async def _household(env: dict[str, Any]) -> None:
    if hid := env.get("household_id"):
        await enqueue_check(hid, f"event:{env['type']}", defer_by=settings.event_debounce_seconds)


async def _instrument(env: dict[str, Any]) -> None:
    """Market events carry no household: re-check every household that holds the instrument."""
    iid = (env.get("payload") or {}).get("instrument_id")
    if not iid:
        return
    async with SessionLocal() as db:
        hids = (await db.execute(select(HoldingCheck.household_id).where(HoldingCheck.instrument_id == iid).distinct())).scalars().all()
    for hid in hids:
        await enqueue_check(str(hid), f"event:{env['type']}", defer_by=settings.event_debounce_seconds)


consumer = EventConsumer("readiness", {
    "advisor.run_done": _household, "advisor.reviews_done": _household, "import.completed": _household,
    "fundamentals.updated": _instrument,
})
