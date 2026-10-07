"""Event-driven re-analysis: ledger changes, imports, preference edits and big price moves
schedule a (debounced) advisor run for the affected household."""
from __future__ import annotations

from typing import Any

from fm_common.db.session import SessionLocal
from fm_common.events import EventConsumer

from . import queue
from .config import settings


async def _rerun(env: dict[str, Any]) -> None:
    hid = env.get("household_id")
    if hid:
        await queue.enqueue("run_household_job", hid, f"event:{env['type']}", job_id=f"rerun:{hid}", defer_by=settings.rerun_debounce_seconds)


async def _price_moved(env: dict[str, Any]) -> None:
    """A big move in a symbol → re-run households that hold it (via recommendations index)."""
    from sqlalchemy import select

    from .models import Recommendation

    sym = env["payload"]["symbol"]
    async with SessionLocal() as db:
        hids = (await db.execute(select(Recommendation.household_id).where(Recommendation.symbol == sym, Recommendation.status != "superseded").distinct())).scalars().all()
    for hid in hids:
        await queue.enqueue("run_household_job", str(hid), "event:price.moved", job_id=f"rerun:{hid}", defer_by=settings.rerun_debounce_seconds)


async def rename_profile(db: Any, profile_id: str, old: str, new: str) -> int:
    """Existing recommendations carry the profile name in their text (headline "Old: Rebalance…",
    cards, full report). Rewrite them now, including decided/snoozed ones a re-run won't touch."""
    import uuid

    from sqlalchemy import select

    from .models import Recommendation

    rows = (await db.execute(select(Recommendation).where(Recommendation.profile_id == uuid.UUID(profile_id)))).scalars().all()
    for r in rows:
        if r.headline.startswith(f"{old}: "):
            r.headline = f"{new}: {r.headline[len(old) + 2:]}"
        if r.scope == "profile" and r.name == old:
            r.name = new
        fr = dict(r.full_report or {})
        if (fr.get("summary") or {}).get("profile") == old:
            fr["summary"] = {**fr["summary"], "profile": new}
            if isinstance(fr["summary"].get("headline"), str) and fr["summary"]["headline"].startswith(f"{old}: "):
                fr["summary"]["headline"] = f"{new}: {fr['summary']['headline'][len(old) + 2:]}"
            r.full_report = fr
        if r.narrative and f"{old}: " in r.narrative:
            r.narrative = r.narrative.replace(f"{old}: ", f"{new}: ")
        sr = dict(r.short_report or {})
        if isinstance(sr.get("headline"), str) and sr["headline"].startswith(f"{old}: "):
            r.short_report = {**sr, "headline": f"{new}: {sr['headline'][len(old) + 2:]}"}
    await db.commit()
    return len(rows)


async def _profile_updated(env: dict[str, Any]) -> None:
    p = env.get("payload") or {}
    if p.get("old_name") and p.get("new_name"):
        async with SessionLocal() as db:
            await rename_profile(db, p["profile_id"], p["old_name"], p["new_name"])
    await _rerun(env)


consumer = EventConsumer("advisor", {
    "txn.recorded": _rerun, "txn.updated": _rerun, "txn.deleted": _rerun, "import.completed": _rerun, "import.deleted": _rerun,
    "holding.prefs_changed": _rerun, "profile.updated": _profile_updated, "price.moved": _price_moved,
})
