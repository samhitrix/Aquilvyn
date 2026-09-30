"""The questions the readiness engine asks about every holding, and what to do when the answer is "no".

Pure functions (no I/O) so every rule is unit-tested:

  fundamentals  Are the company's fundamentals loaded, complete enough and recent?        (stocks)
  technicals    Is there enough recent price history for the technical indicators?        (market-priced assets)
  analysis      Was the advisor's call built with the data we have now?                    (all)
  ai_review     Has an AI reviewed the call's current inputs?                              (when an AI is connected)

Each check is ``ok`` / ``failed`` / ``waiting`` (being fixed, nothing wrong yet) / ``n/a``. A failed check gets a
fix — refresh the data, re-analyse the holding, or re-queue the AI review — retried with a growing back-off.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

TECH_TYPES = {"stock", "etf", "mutual_fund", "reit", "gold"}
FUND_TYPES = {"stock"}
MIN_BARS = 200               # SMA-200 needs 200 closes
MIN_FUND_FIELDS = 4          # of market_svc.api.FUND_READY_FIELDS
FUND_MAX_AGE = timedelta(days=7)
MAX_BAR_AGE = {"mutual_fund": 6}  # days since the last bar (NAVs publish once a day, holidays in between)
DEFAULT_BAR_AGE = 5

CHECKS = ("fundamentals", "technicals", "analysis", "ai_review")
LABEL = {"fundamentals": "Fundamentals", "technicals": "Technicals", "analysis": "Analysis up to date", "ai_review": "AI review"}
FIX_ACTION = {"fundamentals": "refresh_fundamentals", "technicals": "refresh_history", "analysis": "analyse", "ai_review": "review"}


@dataclass
class Check:
    state: str            # ok | failed | waiting | n/a
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"state": self.state, "detail": self.detail}


def _dt(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


def check_fundamentals(asset_type: str, cov: dict[str, Any] | None, now: datetime) -> Check:
    if asset_type not in FUND_TYPES:
        return Check("n/a", "not a company share")
    f = (cov or {}).get("fundamentals")
    if not f:
        return Check("failed", "no company fundamentals loaded yet")
    fields = f.get("fields") or []
    if len(fields) < MIN_FUND_FIELDS:
        return Check("failed", f"only {len(fields)} key figures loaded ({', '.join(fields) or 'none'})")
    as_of = _dt(f.get("as_of"))
    if as_of and now - as_of > FUND_MAX_AGE:
        return Check("failed", f"last loaded {(now - as_of).days} days ago")
    return Check("ok", f"{len(fields)} key figures from {f.get('source') or '?'}")


def check_technicals(asset_type: str, cov: dict[str, Any] | None, today: date) -> Check:
    if asset_type not in TECH_TYPES:
        return Check("n/a", "no market price history for this kind of holding")
    if not cov:
        return Check("failed", "no market data found for this symbol")
    n, last = int(cov.get("bars_400d") or 0), cov.get("last_bar")
    if n < MIN_BARS:
        return Check("failed", f"only {n} days of price history (needs {MIN_BARS})")
    age = (today - date.fromisoformat(last)).days if last else 999
    if age > MAX_BAR_AGE.get(asset_type, DEFAULT_BAR_AGE):
        return Check("failed", f"latest price is {age} days old")
    return Check("ok", f"{n} days of prices, latest {last}")


def check_analysis(h: dict[str, Any], fund: Check, tech: Check, cov: dict[str, Any] | None) -> Check:
    """Stale = the call was built without data that is loaded now (so re-analysing would change it)."""
    rep = h.get("report") or {}
    if fund.state == "ok" and not rep.get("fundamentals_available"):
        return Check("failed", "built before the fundamentals loaded — needs re-analysis")
    if tech.state == "ok" and (rep.get("bars") or 0) < MIN_BARS:
        return Check("failed", f"built with {rep.get('bars') or 0} days of prices; {cov.get('bars_400d') if cov else '?'} are loaded now")
    if fund.state == "failed" or tech.state == "failed":
        return Check("waiting", "will re-analyse once the missing data loads")
    return Check("ok", f"built {h.get('built_at', '')[:16].replace('T', ' ')}")


def check_ai(h: dict[str, Any], providers: list[str], paused: dict[str, str], analysis: Check) -> Check:
    ai = h.get("ai") or {}
    if not providers:
        return Check("n/a", "no AI model connected (Settings → AI providers)")
    if analysis.state == "failed":
        return Check("waiting", "reviewed after the re-analysis")
    said = {"verified": "agreed", "caution": "agreed, with caution", "needs_review": "disagreed — see the report"}.get(ai.get("consensus") or "", "reviewed")
    if ai.get("reviewed"):
        return Check("ok", said)
    if ai.get("earlier_review_at"):  # yesterday's review stays valid until today's lands (re-check is queued by the advisor)
        return Check("ok", f"{said} on {ai['earlier_review_at'][:10]} · re-check with today's data queued")
    if paused and set(paused) >= set(providers):
        who, why = next(iter(paused.items()))
        return Check("failed", f"every AI model is paused ({who}: {why or 'rate limit'})")
    if ai.get("last_error"):
        return Check("failed", str(ai["last_error"])[:300])
    return Check("failed", "not reviewed yet")


def evaluate(h: dict[str, Any], cov: dict[str, Any] | None, providers: list[str], paused: dict[str, str], now: datetime) -> dict[str, Check]:
    fund = check_fundamentals(h["asset_type"], cov, now)
    tech = check_technicals(h["asset_type"], cov, now.date())
    ana = check_analysis(h, fund, tech, cov)
    return {"fundamentals": fund, "technicals": tech, "analysis": ana, "ai_review": check_ai(h, providers, paused, ana)}


def due(fix: dict[str, Any] | None, now: datetime) -> bool:
    return not fix or not fix.get("next_try") or datetime.fromisoformat(fix["next_try"]) <= now


def record_attempt(fix: dict[str, Any] | None, now: datetime, backoff: tuple[int, ...], action: str, error: str | None = None) -> dict[str, Any]:
    """Attempt n waits backoff[n-1] seconds before the next one (the last step repeats)."""
    attempts = int((fix or {}).get("attempts") or 0) + 1
    wait = backoff[min(attempts, len(backoff)) - 1]
    return {"attempts": attempts, "last_try": now.isoformat(), "next_try": (now + timedelta(seconds=wait)).isoformat(),
            "last_action": action, "last_error": error}


def plan_fixes(checks: dict[str, Check], fixes: dict[str, Any], now: datetime) -> list[str]:
    """Which checks to fix in this pass: failed ones whose back-off has run out. Data first — the re-analysis and
    the AI review only make sense on complete data."""
    return [c for c in CHECKS if checks[c].state == "failed" and due(fixes.get(c), now)]


def status_of(checks: dict[str, Check], fixes: dict[str, Any], attention_after: int) -> str:
    failing = [c for c in CHECKS if checks[c].state in ("failed", "waiting")]
    if not failing:
        return "ready"
    if any(int((fixes.get(c) or {}).get("attempts") or 0) >= attention_after for c in failing if checks[c].state == "failed"):
        return "attention"
    return "fixing"
