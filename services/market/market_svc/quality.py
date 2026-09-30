"""Data Quality & Confidence Engine (E34).

Grades every instrument's data so the Advisor never makes a confident call on bad numbers:
    score 0-100 → grade A (≥85) / B (≥70) / C (≥50) / D (<50)
The Advisor caps a recommendation's confidence by this grade and refuses EXIT/ADD calls on D.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np

from .config import settings

KEY_FUNDAMENTALS = ("pe", "pb", "roe", "debt_to_equity", "profit_margin", "revenue_growth", "earnings_growth", "market_cap")


def assess(
    asset_type: str,
    quote: dict[str, Any] | None,
    bars: list[dict[str, Any]],
    fundamentals: dict[str, Any] | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    issues: list[dict[str, Any]] = []
    score = 100.0
    sources: dict[str, Any] = {}

    def flag(severity: str, code: str, msg: str, penalty: float) -> None:
        nonlocal score
        score -= penalty
        issues.append({"severity": severity, "code": code, "message": msg})

    # --- live price ---
    if asset_type in ("stock", "etf", "reit", "index"):
        if not quote:
            flag("high", "no_quote", "No live quote available", 30)
        else:
            ts = datetime.fromisoformat(quote["ts"])
            age = (now - ts).total_seconds()
            sources["quote"] = {"source": quote.get("source"), "as_of": quote["ts"]}
            if age > settings.dq_quote_stale_seconds and _is_market_hours(now):
                flag("medium", "stale_quote", f"Quote is {int(age // 60)} min old during market hours", 10)

    # --- history coverage / gaps / outliers ---
    if asset_type not in ("epf", "vpf", "ppf", "fixed_deposit", "cash", "bond"):
        if len(bars) < 60:
            flag("high", "short_history", f"Only {len(bars)} daily bars; indicators unreliable", 25)
        else:
            sources["history"] = {"source": bars[-1].get("source"), "bars": len(bars), "last": bars[-1]["date"]}
            last = date.fromisoformat(bars[-1]["date"])
            lag = np.busday_count(last, now.date())
            limit = settings.dq_nav_stale_days if asset_type in ("mutual_fund", "nps") else 3
            if lag > limit:
                flag("medium", "stale_history", f"Last close is {lag} business days old", 10)
            expected = np.busday_count(date.fromisoformat(bars[0]["date"]), last) + 1
            missing = max(0, expected - len(bars))
            if expected and missing / expected > 0.08:  # holidays ≈ 5 %
                flag("low", "gaps", f"{missing} missing trading days in history", 5)
            closes = np.array([b["close"] for b in bars], dtype=float)
            rets = np.diff(np.log(closes))
            if len(rets) and np.max(np.abs(rets)) > 0.35:
                flag("medium", "outlier", "A >35% single-day move — possible unadjusted split/bonus", 10)

    # --- fundamentals completeness & freshness ---
    if asset_type == "stock":
        if not fundamentals:
            flag("high", "no_fundamentals", "No fundamentals available", 25)
        else:
            data = fundamentals.get("data", {})
            present = [k for k in KEY_FUNDAMENTALS if data.get(k) is not None]
            completeness = len(present) / len(KEY_FUNDAMENTALS)
            sources["fundamentals"] = {"source": fundamentals.get("source"), "as_of": fundamentals.get("as_of"), "completeness": round(completeness, 2)}
            if completeness < 0.75:
                missing = [k for k in KEY_FUNDAMENTALS if k not in present]
                flag("medium", "incomplete_fundamentals", f"Missing: {', '.join(missing)}", 20 * (1 - completeness))
            as_of = datetime.fromisoformat(fundamentals["as_of"])
            if now - as_of > timedelta(days=settings.dq_fundamentals_stale_days):
                flag("medium", "stale_fundamentals", f"Fundamentals are {(now - as_of).days} days old", 10)
            if fundamentals.get("source") == "simulated":
                issues.append({"severity": "info", "code": "simulated", "message": "Simulated data (offline mode)"})

    score = max(0.0, min(100.0, score))
    if sum(1 for i in issues if i["severity"] == "high") >= 2:
        score = min(score, 45.0)  # e.g. no price history AND no fundamentals: nothing to base a call on → D
    grade = "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 50 else "D"
    return {"score": round(score, 1), "grade": grade, "issues": issues, "sources": sources, "checked_at": now.isoformat()}


def _is_market_hours(now: datetime) -> bool:
    ist = now + timedelta(hours=5, minutes=30)
    return ist.weekday() < 5 and (9 * 60 + 15) <= ist.hour * 60 + ist.minute <= (15 * 60 + 30)
