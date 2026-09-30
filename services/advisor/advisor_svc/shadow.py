"""Shadow Portfolio (E37) and Recommendation Scorecard (E38) — the proof layer.

Shadow: accepting a recommendation records the *virtual* trade it implies. The difference
between 'if you had followed every accepted call' and reality is, per trade,
    qty_delta × (current_price − price_at_accept)
(+ for virtual buys that rose, + for virtual sells that avoided a fall). Nothing is ever
sent to a broker in P1.

Scorecard: every actionable call is re-priced after 30/90/180/365 days and judged against
NIFTY: a bearish call is 'right' if the holding under-performed the index by >2 pp, a bullish
call if it out-performed by >2 pp; HOLDs are right within ±5 pp."""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Recommendation, RecommendationOutcome, ShadowTrade

HORIZONS = (30, 90, 180, 365)
BULL = {"ADD", "ACCUMULATE"}
BEAR = {"EXIT", "TRIM", "SWITCH"}


def shadow_trade_for(rec: Recommendation) -> ShadowTrade | None:
    sugg = (rec.short_report or {}).get("suggestion") or {}
    price = rec.price_at_call
    if not price or not rec.symbol:
        return None
    qty = sugg.get("quantity")
    if qty is None and sugg.get("value"):
        qty = float(sugg["value"]) / float(price)
    if not qty:
        return None
    sign = -1 if sugg.get("type") in ("sell", "switch") else 1 if sugg.get("type") == "buy" else 0
    if not sign:
        return None
    return ShadowTrade(id=uuid.uuid4(), household_id=rec.household_id, recommendation_id=rec.id, profile_id=rec.profile_id,
                       instrument_id=rec.instrument_id, symbol=rec.symbol, qty_delta=Decimal(str(sign * float(qty))), price=price)


def shadow_summary(trades: list[ShadowTrade], quotes: dict[str, Any]) -> dict[str, Any]:
    rows = []
    total = 0.0
    for t in trades:
        q = quotes.get(t.symbol) or {}
        now = q.get("price")
        if now is None:
            continue
        diff = float(t.qty_delta) * (now - float(t.price))
        total += diff
        rows.append({"symbol": t.symbol, "qty_delta": float(t.qty_delta), "price_then": float(t.price), "price_now": now,
                     "executed_on": t.executed_on.isoformat(), "difference": round(diff, 2), "recommendation_id": str(t.recommendation_id)})
    return {"trades": rows, "net_difference": round(total, 2),
            "explanation": "Positive = following the accepted calls would have left you better off than your actual portfolio."}


def verdict(action: str, ret: float, bench: float | None) -> str:
    excess = ret - (bench or 0.0)
    if action in BULL:
        return "right" if excess > 2 else "wrong" if excess < -2 else "neutral"
    if action in BEAR:
        return "right" if excess < -2 else "wrong" if excess > 2 else "neutral"
    return "right" if abs(excess) <= 5 else "neutral"


async def evaluate_outcomes(db: AsyncSession, quotes: dict[str, Any], nifty_now: float | None) -> int:
    today = date.today()
    recs = (await db.execute(select(Recommendation).where(Recommendation.scope == "holding", Recommendation.price_at_call.isnot(None),
                                                          Recommendation.created_at <= today - timedelta(days=min(HORIZONS))))).scalars().all()
    n = 0
    for r in recs:
        q = (quotes.get(r.symbol or "") or {}).get("price")
        if q is None:
            continue
        age = (today - r.created_at.date()).days
        for h in HORIZONS:
            if age < h:
                continue
            ret = (q / float(r.price_at_call) - 1) * 100
            bench = (nifty_now / float(r.benchmark_at_call) - 1) * 100 if nifty_now and r.benchmark_at_call else None
            stmt = insert(RecommendationOutcome).values(
                recommendation_id=r.id, horizon_days=h, household_id=r.household_id, evaluated_on=today, price_then=r.price_at_call,
                price_now=Decimal(str(q)), return_pct=round(ret, 2), benchmark_return_pct=round(bench, 2) if bench is not None else None,
                verdict=verdict(r.action, ret, bench),
            ).on_conflict_do_nothing()
            n += (await db.execute(stmt)).rowcount or 0
    await db.commit()
    return n


async def scorecard(db: AsyncSession, household_id: uuid.UUID) -> dict[str, Any]:
    rows = (await db.execute(
        select(RecommendationOutcome, Recommendation.action, Recommendation.rule_id)
        .join(Recommendation, Recommendation.id == RecommendationOutcome.recommendation_id)
        .where(RecommendationOutcome.household_id == household_id)
    )).all()
    by_rule: dict[str, dict[str, int]] = defaultdict(lambda: {"right": 0, "wrong": 0, "neutral": 0})
    by_h: dict[int, dict[str, int]] = defaultdict(lambda: {"right": 0, "wrong": 0, "neutral": 0})
    for o, _action, rule_id in rows:
        by_rule[rule_id][o.verdict] += 1
        by_h[o.horizon_days][o.verdict] += 1

    def rate(d: dict[str, int]) -> float | None:
        decided = d["right"] + d["wrong"]
        return round(d["right"] / decided * 100, 1) if decided else None

    return {
        "evaluated_calls": len(rows),
        "by_horizon": {h: {**v, "hit_rate_pct": rate(v)} for h, v in sorted(by_h.items())},
        "by_rule": {k: {**v, "hit_rate_pct": rate(v)} for k, v in by_rule.items()},
        "method": "Calls are re-priced at 30/90/180/365 days and compared with NIFTY 50 over the same window.",
    }
