"""Mutual Fund Analytics Engine (E14): rolling returns vs benchmark, consistency, risk-adjusted
returns, cost drag (expense ratio, direct vs regular)."""
from __future__ import annotations

from typing import Any

import numpy as np

from .risk import beta_corr, stats

# Typical category expense ratios (direct plans) — P2 will source live category medians.
CATEGORY_ER = {"equity:index": 0.20, "equity:large_cap": 0.75, "equity:flexi_cap": 0.70, "equity:mid_cap": 0.75,
               "equity:small_cap": 0.75, "equity:elss": 0.75, "debt:corporate_bond": 0.35, "debt:liquid": 0.20}


def _rolling(nav: np.ndarray, bench: np.ndarray, window: int) -> dict[str, Any] | None:
    if len(nav) <= window + 20:
        return None
    years = window / 252
    f = (nav[window:] / nav[:-window]) ** (1 / years) - 1
    b = (bench[window:] / bench[:-window]) ** (1 / years) - 1
    return {
        "window_years": round(years, 1), "fund_avg_pct": round(float(f.mean()) * 100, 2),
        "bench_avg_pct": round(float(b.mean()) * 100, 2), "fund_min_pct": round(float(f.min()) * 100, 2),
        "beat_benchmark_pct": round(float((f > b).mean()) * 100, 1), "observations": int(len(f)),
    }


def analyse(bars: list[dict[str, Any]], bench_bars: list[dict[str, Any]] | None, meta: dict[str, Any]) -> dict[str, Any]:
    if len(bars) < 60:
        return {"available": False, "reason": f"Only {len(bars)} NAV points"}
    out: dict[str, Any] = {"available": True, "risk": stats(bars), "category": meta.get("mf_category"), "plan": meta.get("plan")}
    rolling: dict[str, Any] = {}
    if bench_bars:
        bm = {b["date"]: b["close"] for b in bench_bars}
        pairs = np.array([(b["close"], bm[b["date"]]) for b in bars if b["date"] in bm], dtype=float)
        if len(pairs) > 100:
            nav, bench = pairs[:, 0], pairs[:, 1]
            for label, w in (("1y", 252), ("3y", 756), ("5y", 1260)):
                if (r := _rolling(nav, bench, w)) is not None:
                    rolling[label] = r
            out["benchmark_risk"] = stats([{"close": c} for c in bench])
            out.update(beta_corr(bars, bench_bars))
            # Jensen's alpha (annualised, rf 6.5 %)
            fr, br = out["risk"].get("cagr_pct"), out["benchmark_risk"].get("cagr_pct")
            if fr is not None and br is not None and out.get("beta") is not None:
                out["alpha_pct"] = round(fr - (6.5 + out["beta"] * (br - 6.5)), 2)
            dn = np.diff(np.log(bench)) < 0
            if dn.sum() > 10:
                fret, bret = np.diff(np.log(nav))[dn], np.diff(np.log(bench))[dn]
                out["downside_capture_pct"] = round(float(fret.mean() / bret.mean()) * 100, 1)
    out["rolling"] = rolling
    main = rolling.get("3y") or rolling.get("1y")
    out["consistency_pct"] = main["beat_benchmark_pct"] if main else None

    er = meta.get("expense_ratio")
    cat_er = CATEGORY_ER.get(meta.get("mf_category") or "", 0.75)
    out["cost"] = {
        "expense_ratio_pct": er, "category_typical_pct": cat_er,
        "is_regular_plan": meta.get("plan") == "regular",
        "excess_cost_pct": round(er - cat_er, 2) if er is not None else None,
    }

    # Score: consistency 35 · alpha 25 · risk-adjusted 20 · cost 20
    parts: list[tuple[float, float]] = []
    if out["consistency_pct"] is not None:
        parts.append((0.35, min(100.0, out["consistency_pct"] * 1.25)))
    if (a := out.get("alpha_pct")) is not None:
        parts.append((0.25, float(np.clip(50 + a * 12.5, 0, 100))))
    if (s := out["risk"].get("sortino")) is not None:
        parts.append((0.20, float(np.clip(s * 50, 0, 100))))
    if er is not None:
        cost = 100 - max(0.0, er - cat_er) * 100 - (30 if meta.get("plan") == "regular" else 0)
        parts.append((0.20, float(np.clip(cost, 0, 100))))
    w = sum(p[0] for p in parts)
    out["score"] = round(sum(p[0] * p[1] for p in parts) / w, 1) if w else None
    out["verdict"] = None if out["score"] is None else "top" if out["score"] >= 70 else "good" if out["score"] >= 55 else "average" if out["score"] >= 40 else "laggard"
    return out
