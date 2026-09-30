"""Scoring Engine (E19): per-dimension 0-100 scores weighted by holding intent."""
from __future__ import annotations

from typing import Any

WEIGHTS: dict[str, dict[str, float]] = {
    "core": {"fundamental": 0.55, "technical": 0.15, "risk": 0.20, "tax": 0.10},
    "satellite": {"fundamental": 0.40, "technical": 0.35, "risk": 0.15, "tax": 0.10},
    "trade": {"technical": 0.70, "risk": 0.20, "fundamental": 0.10},
    "retirement": {"fund_quality": 0.6, "risk": 0.4},
    "goal": {"fundamental": 0.4, "technical": 0.2, "risk": 0.3, "tax": 0.1},
    "fund": {"fund_quality": 0.65, "risk": 0.20, "tax": 0.15},
}


def dimension_scores(facts: dict[str, Any], evidence: dict[str, Any]) -> dict[str, float | None]:
    risk = 100.0
    if facts.get("pos.over_cap"):
        risk -= 35
    vol = ((evidence.get("risk") or {}).get("volatility_pct")) or 0
    risk -= max(0.0, vol - 25) * 1.2
    if facts.get("dd.triggered"):
        risk -= 20
    tax = evidence.get("tax") or {}
    tax_score = None
    if tax.get("applicable"):
        tax_score = 100.0
        if tax.get("days_to_ltcg") is not None and (tax.get("saving_if_wait") or 0) > 0:
            tax_score = 60.0  # selling now would be tax-inefficient
    return {
        "technical": facts.get("tech.score"),
        "fundamental": facts.get("fund.score"),
        "fund_quality": facts.get("mf.score"),
        "risk": round(max(0.0, min(100.0, risk)), 1),
        "tax": tax_score,
    }


def composite(intent: str, asset_type: str, dims: dict[str, float | None]) -> dict[str, Any]:
    key = "fund" if asset_type in ("mutual_fund", "nps") else intent if intent in WEIGHTS else "satellite"
    weights = WEIGHTS[key]
    used = {d: w for d, w in weights.items() if dims.get(d) is not None}
    total = sum(used.values())
    score = round(sum(dims[d] * w for d, w in used.items()) / total, 1) if total else None  # type: ignore[operator]
    return {"score": score, "weights": weights, "weights_used": {d: round(w / total, 3) for d, w in used.items()} if total else {}, "profile": key}
