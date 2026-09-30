"""Fundamental Analysis Engine (E13) → 0-100 score across Valuation / Quality / Growth /
Balance-sheet / Governance, each metric with the threshold it was judged against."""
from __future__ import annotations

from typing import Any

# (key, label, pillar, direction, good, bad, unit)
#   direction +1: higher is better (score 100 at >= good, 0 at <= bad); -1: lower is better
RULES: list[tuple[str, str, str, int, float, float, str]] = [
    ("roe", "Return on equity", "quality", 1, 20, 8, "%"),
    ("roce", "Return on capital employed", "quality", 1, 20, 10, "%"),
    ("operating_margin", "Operating margin", "quality", 1, 20, 5, "%"),
    ("profit_margin", "Net profit margin", "quality", 1, 15, 3, "%"),
    ("revenue_growth", "Revenue growth (YoY)", "growth", 1, 15, 0, "%"),
    ("earnings_growth", "Earnings growth (YoY)", "growth", 1, 18, -5, "%"),
    ("revenue_cagr_3y", "Revenue CAGR 3Y", "growth", 1, 15, 3, "%"),
    ("eps_cagr_3y", "EPS CAGR 3Y", "growth", 1, 18, 0, "%"),
    ("debt_to_equity", "Debt / equity", "balance_sheet", -1, 0.3, 1.5, "x"),
    ("interest_coverage", "Interest coverage", "balance_sheet", 1, 8, 2, "x"),
    ("current_ratio", "Current ratio", "balance_sheet", 1, 1.5, 0.8, "x"),
    ("peg", "PEG ratio", "valuation", -1, 1.0, 3.0, "x"),
    ("promoter_pledge", "Promoter shares pledged", "governance", -1, 0, 20, "%"),
    ("promoter_holding", "Promoter holding", "governance", 1, 50, 25, "%"),
]

PILLAR_WEIGHTS = {"quality": 0.30, "growth": 0.25, "valuation": 0.20, "balance_sheet": 0.15, "governance": 0.10}


def _linear(v: float, direction: int, good: float, bad: float) -> float:
    if direction > 0:
        return 100.0 if v >= good else 0.0 if v <= bad else (v - bad) / (good - bad) * 100
    return 100.0 if v <= good else 0.0 if v >= bad else (bad - v) / (bad - good) * 100


def analyse(fund: dict[str, Any] | None, sector: str | None = None) -> dict[str, Any]:
    if not fund or not fund.get("data"):
        return {"available": False, "reason": "No fundamentals"}
    d = fund["data"]
    metrics: list[dict[str, Any]] = []
    for key, label, pillar, direction, good, bad, unit in RULES:
        v = d.get(key)
        if v is None:
            continue
        s = _linear(float(v), direction, good, bad)
        metrics.append({
            "key": key, "label": label, "pillar": pillar, "value": v, "unit": unit, "score": round(s, 1),
            "threshold": f"{'≥' if direction > 0 else '≤'}{good}{unit} good · {'≤' if direction > 0 else '≥'}{bad}{unit} weak",
            "status": "good" if s >= 70 else "ok" if s >= 40 else "weak",
        })

    # Relative valuation: vs own 5Y median and vs sector
    pe, pe5, spe = d.get("pe"), d.get("pe_5y_median"), d.get("sector_pe")
    if pe and pe > 0:
        for ref, label in ((pe5, "P/E vs own 5Y median"), (spe, "P/E vs sector")):
            if ref:
                prem = (pe / ref - 1) * 100
                s = _linear(prem, -1, -10, 40)
                metrics.append({"key": f"pe_premium_{'hist' if ref is pe5 else 'sector'}", "label": label, "pillar": "valuation",
                                "value": round(prem, 1), "unit": "% premium", "score": round(s, 1),
                                "threshold": "≤-10% cheap · ≥+40% expensive", "status": "good" if s >= 70 else "ok" if s >= 40 else "weak",
                                "detail": f"P/E {pe:.1f} vs {ref:.1f}"})
    elif pe is not None and pe <= 0:
        metrics.append({"key": "pe_negative", "label": "Loss-making (negative P/E)", "pillar": "valuation", "value": pe, "unit": "x",
                        "score": 0.0, "threshold": "P/E must be positive", "status": "weak"})

    pillars: dict[str, dict[str, Any]] = {}
    for p in PILLAR_WEIGHTS:
        ms = [m for m in metrics if m["pillar"] == p]
        pillars[p] = {"score": round(sum(m["score"] for m in ms) / len(ms), 1) if ms else None, "metrics": len(ms)}
    avail = {p: v["score"] for p, v in pillars.items() if v["score"] is not None}
    wsum = sum(PILLAR_WEIGHTS[p] for p in avail) or 1
    score = round(sum(PILLAR_WEIGHTS[p] * s for p, s in avail.items()) / wsum, 1) if avail else None

    red_flags = []
    if (pl := d.get("promoter_pledge")) is not None and pl >= 10:
        red_flags.append({"code": "pledge", "message": f"{pl:.1f}% of promoter shares pledged"})
    if (de := d.get("debt_to_equity")) is not None and de >= 2:
        red_flags.append({"code": "leverage", "message": f"High leverage: D/E {de:.2f}x"})
    if (eg := d.get("earnings_growth")) is not None and eg <= -20:
        red_flags.append({"code": "earnings_collapse", "message": f"Earnings down {eg:.0f}% YoY"})
    if pe is not None and pe <= 0:
        red_flags.append({"code": "losses", "message": "Company is loss-making"})

    verdict = None if score is None else "strong" if score >= 70 else "healthy" if score >= 55 else "average" if score >= 40 else "weak"
    return {
        "available": score is not None, "score": score, "verdict": verdict, "pillars": pillars, "metrics": metrics,
        "red_flags": red_flags, "coverage": round(len(metrics) / (len(RULES) + 2), 2), "source": fund.get("source"), "as_of": fund.get("as_of"),
    }
