"""Narration & Ask-my-portfolio (E25). Always grounded in structured data; template fallback."""
from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from fm_common.logging import get_logger

from ..models import Recommendation
from .base import ASK_SYSTEM, NARRATE_SYSTEM
from .registry import complete_with_fallback, household_clients

log = get_logger(__name__)


def template(rec: Recommendation) -> str:
    s = rec.short_report
    parts = [f"{s['headline']}.", " ".join(r.rstrip(".") + "." for r in s.get("reasons", [])[:3])]
    if s.get("tax_impact"):
        parts.append(s["tax_impact"])
    parts.append(f"What would change this: {s['what_would_change']}")
    return " ".join(p for p in parts if p)


async def narrate(db: AsyncSession, household_id: uuid.UUID, recs: list[Recommendation]) -> int:
    chain, _ = await household_clients(db, household_id)
    n = 0
    for rec in recs:
        text = None
        if chain:
            reviewer = ((rec.short_report or {}).get("ai_view") or {}).get("provider")
            order = [c for c in chain if c.provider != reviewer] + [c for c in chain if c.provider == reviewer]  # summary ≠ reviewer
            text, used, errors = await complete_with_fallback(household_id, order, NARRATE_SYSTEM,
                                                              "Packet:\n" + json.dumps(rec.short_report, default=str, ensure_ascii=False), 1500)
            if errors:
                log.warning("ai.narrate_fallback", errors=[f"{e['provider']}: {e['error'][:80]}" for e in errors])
                chain = [c for c in chain if c.provider not in {e["provider"] for e in errors}] or chain[:0]
        rec.narrative = (text or template(rec)).strip()
        n += 1
    await db.commit()
    return n


async def ask(db: AsyncSession, household_id: uuid.UUID, question: str, context: dict[str, Any]) -> dict[str, Any]:
    chain, _ = await household_clients(db, household_id)
    if not chain:
        return {"answer": None, "mode": "rules_only",
                "message": "No AI provider available — add one in Settings → AI providers (or wait: providers out of quota are paused for 24 h)."}
    user = f"Context JSON:\n{json.dumps(context, default=str, ensure_ascii=False)}\n\nQuestion: {question}"
    answer, used, errors = await complete_with_fallback(household_id, chain, ASK_SYSTEM, user, 4000)
    if used is None:
        detail = "; ".join(f"{e['provider']}: HTTP {e.get('status')} {e['error'][:120]}" for e in errors)
        return {"answer": None, "mode": "ai_failed", "message": f"Every AI provider failed — {detail}"}
    return {"answer": answer, "provider": used.provider, "model": used.model,
            "fallback_from": [e["provider"] for e in errors] or None}
