"""One pass of the readiness engine over a household:

1. ask the advisor what each open holding call was built with (and whether an AI reviewed it);
2. ask market what data is stored for those holdings right now (DB only, one request);
3. evaluate the checks (``checks.py``);
4. fix what failed and is due: refresh the stock's data in market → re-check → re-analyse exactly the holdings
   whose call is now stale → re-queue their AI review;
5. store every holding's verdict and the fix history.

The engine owns no market data and no calls — it only asks the services that do. Runs every 15 minutes, after
every advisor run / AI review batch / import / fundamentals update, and on "Check now".
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from fm_common import http
from fm_common.config import settings as common
from fm_common.identity.tokens import create_service_token
from fm_common.logging import get_logger
from fm_common.redact import redact

from . import checks as ck
from .config import settings
from .models import HoldingCheck, Sweep

log = get_logger(__name__)


NAMES = {common.advisor_url: "the advisor", common.market_url: "market data", common.portfolio_url: "the portfolio service"}


def _err(exc: BaseException) -> str:
    """Readable: "the advisor answered HTTP 503 (…)" / "market data didn't answer" — not an exception dump."""
    if isinstance(exc, http.ServiceError):
        detail = exc.detail.get("detail") if isinstance(exc.detail, dict) else exc.detail
        return redact(f"{NAMES.get(exc.service, exc.service)} answered HTTP {exc.status}" + (f" ({str(detail)[:160]})" if detail else ""))[:300]
    if isinstance(exc, httpx.ConnectError | httpx.TimeoutException | TimeoutError):
        return "a service didn't answer in time (is everything running? python scripts/fm.py ps)"
    return redact(f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)[:300]


async def _coverage(symbols: list[str], token: str) -> dict[str, Any]:
    if not symbols:
        return {}
    res = await http.post(common.market_url, "/api/v1/market/coverage", token=token, json={"symbols": symbols})
    return res.get("instruments") or {}


async def _refresh(action: str, symbol: str, token: str) -> str | None:
    """Ask market to fetch this stock's data again now. Returns an error text, or None when it worked."""
    path = (f"/api/v1/market/fundamentals/{quote(symbol, safe='')}" if action == "refresh_fundamentals"
            else f"/api/v1/market/history/{quote(symbol, safe='')}")
    params = {"refresh": "true"} if action == "refresh_fundamentals" else {"refresh": "true", "days": 400}
    try:
        res = await http.get(common.market_url, path, token=token, params=params, request_timeout=60)
    except Exception as exc:
        return _err(exc)
    if action == "refresh_fundamentals" and not res.get("fundamentals"):
        return "every fundamentals source came back empty (see Settings → Data sources → Test all sources)"
    if action == "refresh_history" and not res.get("bars"):
        return "no price history returned by any source"
    return None


async def check_household(db: AsyncSession, household_id: uuid.UUID, trigger: str = "scheduled") -> dict[str, Any]:
    sweep = Sweep(id=uuid.uuid4(), household_id=household_id, trigger=trigger[:64])
    db.add(sweep)
    await db.commit()
    try:
        stats = await _pass(db, household_id)
    except Exception as exc:
        sweep.status, sweep.error, sweep.finished_at = "failed", _err(exc), datetime.now(UTC)
        await db.commit()
        log.warning("readiness.sweep_failed", household_id=str(household_id), error=sweep.error)
        raise
    sweep.status, sweep.stats, sweep.finished_at = "done", stats, datetime.now(UTC)
    await db.commit()
    log.info("readiness.sweep_done", household_id=str(household_id), trigger=trigger, **{k: v for k, v in stats.items() if isinstance(v, int)})
    return stats


async def _pass(db: AsyncSession, household_id: uuid.UUID) -> dict[str, Any]:
    token = create_service_token("readiness", household_id, ttl=900)
    inp = await http.get(common.advisor_url, "/api/v1/advisor/internal/readiness-input", token=token, request_timeout=30)
    holdings = [h for h in inp.get("holdings") or [] if h.get("instrument_id") and h.get("symbol")]
    providers, paused = inp.get("ai_providers") or [], inp.get("ai_paused") or {}
    symbols = sorted({h["symbol"] for h in holdings})
    cov = await _coverage(symbols, token)
    existing = {(str(r.profile_id), str(r.instrument_id)): r for r in (await db.execute(
        select(HoldingCheck).where(HoldingCheck.household_id == household_id))).scalars()}
    now = datetime.now(UTC)

    # ---- 1. data fixes (per stock, shared by everyone who holds it) ----
    evaluated = {id(h): ck.evaluate(h, cov.get(h["symbol"]), providers, paused, now) for h in holdings}
    fixes_of = {id(h): dict((existing.get((str(h["profile_id"]), h["instrument_id"])) or HoldingCheck(fixes={})).fixes or {}) for h in holdings}
    data_todo: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for h in holdings:
        for c in ck.plan_fixes(evaluated[id(h)], fixes_of[id(h)], now):
            if c in ("fundamentals", "technicals"):
                data_todo.setdefault((ck.FIX_ACTION[c], h["symbol"]), []).append(h)
    sem = asyncio.Semaphore(settings.max_parallel_fixes)

    async def run(action: str, symbol: str) -> tuple[str, str, str | None]:
        async with sem:
            return action, symbol, await _refresh(action, symbol, token)

    todo = list(data_todo)[: settings.max_data_fixes_per_pass]
    results = await asyncio.gather(*(run(a, s) for a, s in todo))
    for action, symbol, error in results:
        check = "fundamentals" if action == "refresh_fundamentals" else "technicals"
        for h in data_todo[(action, symbol)]:
            fixes_of[id(h)][check] = ck.record_attempt(fixes_of[id(h)].get(check), now, settings.backoff_seconds, action, error)
    if results:  # re-read what's stored now and re-evaluate, so a stale call is caught in this same pass
        cov = await _coverage(symbols, token)
        evaluated = {id(h): ck.evaluate(h, cov.get(h["symbol"]), providers, paused, now) for h in holdings}

    # ---- 2. re-analyse exactly the holdings whose call is stale ----
    by_profile: dict[str | None, list[dict[str, Any]]] = {}
    for h in holdings:
        if "analysis" in ck.plan_fixes(evaluated[id(h)], fixes_of[id(h)], now):
            by_profile.setdefault(h.get("profile_id"), []).append(h)
    analysed = 0
    for pid, hs in by_profile.items():
        err = None
        try:
            await http.post(common.advisor_url, "/api/v1/advisor/internal/analyse", token=token,
                            json={"instrument_ids": sorted({h["instrument_id"] for h in hs}), "profile_id": pid, "reason": "readiness"})
            analysed += len(hs)
        except Exception as exc:
            err = _err(exc)
        for h in hs:
            fixes_of[id(h)]["analysis"] = ck.record_attempt(fixes_of[id(h)].get("analysis"), now, settings.backoff_seconds, "analyse", err)

    # ---- 3. re-queue AI reviews that are missing or failed ----
    review = [h for h in holdings if "ai_review" in ck.plan_fixes(evaluated[id(h)], fixes_of[id(h)], now)
              and not (paused and set(paused) >= set(providers))]
    if review:
        err = None
        try:
            await http.post(common.advisor_url, "/api/v1/advisor/review-all", token=token, json={"rec_ids": [h["rec_id"] for h in review]})
        except Exception as exc:
            err = _err(exc)
        for h in review:
            fixes_of[id(h)]["ai_review"] = ck.record_attempt(fixes_of[id(h)].get("ai_review"), now, settings.backoff_seconds, "review",
                                                             err or (h.get("ai") or {}).get("last_error"))

    # ---- 4. store the verdicts ----
    seen: set[tuple[str, str]] = set()
    counts = {"ready": 0, "fixing": 0, "attention": 0}
    failing = dict.fromkeys(ck.CHECKS, 0)
    for h in holdings:
        key = (str(h["profile_id"]), h["instrument_id"])
        seen.add(key)
        checks = evaluated[id(h)]
        fixes = {c: f for c, f in fixes_of[id(h)].items() if checks.get(c) and checks[c].state != "ok"}  # fixed → forget the history
        status = ck.status_of(checks, fixes, settings.attention_after_attempts)
        counts[status] += 1
        for c in ck.CHECKS:
            failing[c] += checks[c].state in ("failed", "waiting")
        row = existing.get(key)
        if row is None:
            row = HoldingCheck(id=uuid.uuid4(), household_id=household_id, profile_id=uuid.UUID(h["profile_id"]) if h.get("profile_id") else None,
                               instrument_id=uuid.UUID(h["instrument_id"]))
            db.add(row)
        row.rec_id = uuid.UUID(h["rec_id"])
        row.symbol, row.name, row.asset_type, row.profile_name = h["symbol"], h.get("name"), h["asset_type"], h.get("profile_name")
        row.status, row.checks, row.fixes, row.checked_at = status, {c: v.as_dict() for c, v in checks.items()}, fixes, now
    gone = [r.id for k, r in existing.items() if k not in seen]
    if gone:
        await db.execute(delete(HoldingCheck).where(HoldingCheck.id.in_(gone)))
    await db.commit()
    return {"holdings": len(holdings), **counts, "failing": failing, "data_fixes": len(results), "reanalysed": analysed,
            "reviews_queued": len(review), "ai_providers": providers}
