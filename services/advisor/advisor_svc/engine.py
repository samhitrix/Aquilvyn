"""Advisor run orchestration: holdings → signals → intent → drawdown → rules → scores → reports
→ priorities → persistence (versioned) → (async) multi-AI review + narration."""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fm_common import http
from fm_common.config import settings as common
from fm_common.events import publish
from fm_common.identity.tokens import create_service_token
from fm_common.logging import get_logger

from . import facts as factmod
from . import health, priority, report, rules, scoring
from . import intent as intentmod
from .config import settings
from .models import AdvisorRun, Recommendation, RecommendationOutcome

log = get_logger(__name__)
ACCRUAL = {"epf", "vpf", "ppf", "fixed_deposit", "bond", "cash"}


async def mark_failed(household_id: uuid.UUID, exc: BaseException) -> None:
    """A run that crashed is recorded as failed with the reason (it used to stay 'running' forever)."""
    from fm_common.db.session import SessionLocal

    try:
        async with SessionLocal() as db:
            run = (await db.execute(select(AdvisorRun).where(AdvisorRun.household_id == household_id, AdvisorRun.status == "running")
                                    .order_by(AdvisorRun.started_at.desc()).limit(1))).scalar_one_or_none()
            if run is not None:
                run.status, run.finished_at = "failed", datetime.now(UTC)
                run.stats = {**(run.stats or {}), "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
                await db.commit()
    except Exception as e2:
        log.warning("advisor.mark_failed_failed", error=str(e2)[:200])


async def _analysis(symbols: list[str], token: str) -> dict[str, dict[str, Any]]:
    sem = asyncio.Semaphore(6)

    async def one(sym: str) -> tuple[str, dict[str, Any]]:
        async with sem:
            try:
                return sym, await http.get(common.analytics_url, f"/api/v1/analysis/instrument/{sym}", token=token, request_timeout=60)
            except Exception as exc:
                log.warning("advisor.analysis_failed", symbol=sym, error=str(exc)[:200])
                return sym, {"data_quality": {"grade": "D", "score": 0, "issues": [{"severity": "high", "code": "analysis_failed", "message": str(exc)[:200]}]}}

    return dict(await asyncio.gather(*(one(s) for s in symbols)))


async def _history(db: AsyncSession, household_id: uuid.UUID, subject: str) -> list[dict[str, Any]]:
    rows = (await db.execute(
        select(Recommendation).where(Recommendation.household_id == household_id, Recommendation.subject_key == subject)
        .order_by(Recommendation.created_at.desc()).limit(6)
    )).scalars().all()
    out = []
    for r in rows:
        outcomes = (await db.execute(select(RecommendationOutcome).where(RecommendationOutcome.recommendation_id == r.id))).scalars().all()
        out.append({"id": str(r.id), "date": r.created_at.date().isoformat(), "action": r.action, "status": r.status,
                    "price_at_call": float(r.price_at_call) if r.price_at_call else None,
                    "outcomes": [{"days": o.horizon_days, "return_pct": o.return_pct, "benchmark_pct": o.benchmark_return_pct, "verdict": o.verdict} for o in outcomes]})
    return out


WATCH_RULES = {"thesis_failed", "thesis_failed_weak_trend", "thesis_failed_persistent", "weak_business_core", "weak_business_core_exit"}


async def _watch_days(db: AsyncSession, household_id: uuid.UUID, subject: str) -> int:
    """Days this holding has been on a "sell half now, the rest if it doesn't recover" call without a break."""
    rows = (await db.execute(
        select(Recommendation.rule_id, Recommendation.created_at).where(Recommendation.household_id == household_id, Recommendation.subject_key == subject)
        .order_by(Recommendation.created_at.desc()).limit(20)
    )).all()
    start = None
    for rule_id, created in rows:
        if rule_id not in WATCH_RULES:
            break
        start = created
    return max(0, (datetime.now(UTC) - start).days) if start else 0


async def _persist(db: AsyncSession, household_id: uuid.UUID, run_id: uuid.UUID, item: dict[str, Any]) -> tuple[Recommendation, str]:
    prev = (await db.execute(
        select(Recommendation).where(Recommendation.household_id == household_id, Recommendation.subject_key == item["subject_key"],
                                     Recommendation.status != "superseded").order_by(Recommendation.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    fields = {k: item[k] for k in (
        "scope", "profile_id", "instrument_id", "symbol", "name", "asset_type", "action", "horizon", "intent", "confidence", "priority",
        "bucket", "actionable", "rupee_impact", "headline", "short_report", "full_report", "input_hash", "rulebook_version", "rule_id",
        "price_at_call", "benchmark_at_call")}
    # compare the RULES' action: the AI review may have turned a sell into a hold on the previous record
    prev_rule = (prev.short_report or {}).get("rule_action", prev.action) if prev is not None else None
    if prev is not None and prev_rule == item["short_report"].get("rule_action", item["action"]):
        changed = prev.input_hash != item["input_hash"]
        keep_price = {"price_at_call": prev.price_at_call, "benchmark_at_call": prev.benchmark_at_call}
        for k, v in {**fields, **keep_price}.items():
            setattr(prev, k, v)
        prev.run_id = run_id
        if changed or prev.ai_consensus not in ("pending", "rules_only"):
            # inputs changed: the last AI review stays valid (and shown) until the re-check lands
            from .ai.review import carry_review

            await carry_review(db, prev)
        if prev.status == "snoozed" and prev.snooze_until and prev.snooze_until <= date.today():
            prev.status = "open"
        return prev, "updated" if changed else "unchanged"
    rec = Recommendation(id=uuid.uuid4(), household_id=household_id, run_id=run_id, subject_key=item["subject_key"], status="open", **fields)
    if prev is not None:
        prev.status = "superseded"
        rec.supersedes_id = prev.id
        rec.change_reason = f"Changed from {prev.action} to {item['action']}: {item['short_report']['reasons'][0] if item['short_report'].get('reasons') else ''}"
        rec.short_report = {**rec.short_report, "changed_from": prev.action, "change_reason": rec.change_reason}
    db.add(rec)
    return rec, "new"


def run_scope(profile_id: uuid.UUID | None, asset_types: list[str] | None, symbols: list[str] | None = None) -> str:
    base = f"profile:{profile_id}" if profile_id else "household"
    if symbols:
        what = symbols[0] if len(symbols) == 1 else f"{len(symbols)} holdings"
        return f"{base}|{what}"[:64]
    return f"{base}|{','.join(sorted(asset_types))}"[:64] if asset_types else base


async def run_household(db: AsyncSession, household_id: uuid.UUID, token: str, trigger: str = "manual", profile_id: uuid.UUID | None = None,
                        asset_types: list[str] | None = None, other_assets: bool = False,
                        instrument_ids: list[uuid.UUID] | None = None) -> dict[str, Any]:
    """Analyse a household, one person (``profile_id``), some kinds of holding (``asset_types``, or
    ``other_assets`` = everything except stocks, ETFs and mutual funds) or exactly some holdings
    (``instrument_ids`` — "analyse this one stock")."""
    book = rules.load_rulebook()
    only_ids = {str(i) for i in instrument_ids or []}
    run = AdvisorRun(id=uuid.uuid4(), household_id=household_id, trigger=trigger, rulebook_version=book.version,
                     scope=run_scope(profile_id, (asset_types or []) + (["other"] if other_assets else [])))
    db.add(run)
    await db.commit()

    # the advisor waits longer for prices than a page does — a first run shouldn't be skipped just
    # because NAVs were still downloading
    params = {**({"scope": "profile", "id": str(profile_id)} if profile_id else {"scope": "household"}), "price_wait": 20}
    holdings_, prefs_list, regime = await asyncio.gather(
        http.get(common.portfolio_url, "/api/v1/holdings", token=token, params=params, request_timeout=40),
        http.get(common.portfolio_url, "/api/v1/holding-prefs", token=token),
        http.get(common.analytics_url, "/api/v1/analysis/regime", token=token),
    )
    rows = [r for r in holdings_["holdings"] if r["quantity"] > 0]
    filtered = bool(asset_types or other_assets or only_ids)

    def wanted(asset_type: str | None, instrument_id: Any = None) -> bool:
        if not filtered:
            return True
        if asset_type is None:
            return False  # portfolio-level findings need the whole portfolio: not part of a filtered run
        if only_ids:
            return str(instrument_id) in only_ids
        return asset_type in (asset_types or []) or (other_assets and asset_type not in ("stock", "etf", "mutual_fund"))
    all_rows = rows  # position weights are always measured against the person's whole portfolio
    rows = [r for r in rows if wanted(r["asset_type"], r["instrument_id"])]
    if only_ids:
        run.scope = run_scope(profile_id, None, sorted({r["symbol"] for r in rows}) or ["no matching holding"])
    # Data gate: never replace a good analysis with one computed on missing prices. Holdings
    # without a live/recent price are skipped; if they are a large part of the portfolio, the
    # whole run is skipped and the previous recommendations stay in place.
    unpriced = [r for r in rows if r["asset_type"] not in ACCRUAL and not r.get("priced")]
    total_value = sum(r["market_value"] for r in rows) or 1.0
    unpriced_share = sum(r["market_value"] for r in unpriced) / total_value
    if rows and unpriced_share > 0.3:
        reason = (f"Live prices unavailable for {len(unpriced)} of {len(rows)} holdings ({unpriced_share:.0%} of value) — "
                  "kept the previous analysis. Check Settings → Data sources, then run again.")
        run.status, run.finished_at = "skipped", datetime.now(UTC)
        run.stats = {"skipped_reason": reason, "unpriced": len(unpriced), "holdings": len(rows)}
        await db.commit()
        log.warning("advisor.run_skipped", run_id=str(run.id), unpriced=len(unpriced), holdings=len(rows))
        return {"run_id": str(run.id), "rulebook_version": book.version, "skipped": True, "reason": reason, "to_review": [], **run.stats}
    skipped_unpriced = [r["name"] or r["symbol"] for r in unpriced]
    rows = [r for r in rows if r not in unpriced]
    prefs = {(p["profile_id"], p["instrument_id"]): p for p in prefs_list}
    profiles = holdings_.get("profile_settings") or {}
    per_profile_value: dict[str, float] = {}
    for r in all_rows:
        if r["asset_type"] not in ACCRUAL and not r.get("priced"):
            continue
        per_profile_value[r["profile_id"]] = per_profile_value.get(r["profile_id"], 0.0) + r["market_value"]

    symbols = sorted({r["symbol"] for r in rows if r["asset_type"] not in ACCRUAL})
    analyses = await _analysis(symbols, token)
    nifty = (regime.get("metrics") or {}).get("nifty")

    items: list[dict[str, Any]] = []
    verdicts: dict[str, dict[str, str]] = {}
    auto_intents: list[dict[str, Any]] = []
    for row in rows:
        pid, iid = row["profile_id"], row["instrument_id"]
        profile = profiles.get(pid) or {}
        pref = prefs.get((pid, iid))
        analysis = analyses.get(row["symbol"]) or {"data_quality": {"grade": "A", "score": 95, "issues": [], "sources": {"valuation": "accrual"}}}
        if pref and pref.get("intent"):
            intent, intent_note = pref["intent"], "Set by you" if pref.get("intent_source") == "user" else "Auto-classified earlier"
        else:
            intent, intent_note = intentmod.infer(row, analysis, len(row.get("lots") or []))
            auto_intents.append({"profile_id": pid, "instrument_id": iid, "intent": intent})
        pv = per_profile_value.get(pid, 0.0)
        f, ev = factmod.build(row, analysis, None, profile, pv, pref, intent, regime)
        if f["dd.triggered"] and row["asset_type"] == "stock":
            since = factmod.attribution_since(ev["drawdown"])
            if since:
                try:
                    attr = await http.post(common.analytics_url, "/api/v1/analysis/attribution", token=token, json={"symbol": row["symbol"], "since_date": since})
                    f, ev = factmod.build(row, analysis, attr, profile, pv, pref, intent, regime)
                except Exception as exc:
                    log.warning("advisor.attribution_failed", symbol=row["symbol"], error=str(exc)[:200])
        subject = f"holding:{pid}:{iid}"
        f["watch.days"] = await _watch_days(db, household_id, subject)
        decision = rules.evaluate(f, book)
        dims = scoring.dimension_scores(f, ev)
        scores = {"dimensions": dims, "composite": scoring.composite(intent, row["asset_type"], dims)}
        rep = report.build(
            decision=decision, row=row, facts=f, evidence=ev, scores=scores, intent=intent, intent_note=intent_note,
            rulebook_version=book.version, profile_value=pv, profile_name=profile.get("display_name"),
            history=await _history(db, household_id, subject),
        )
        rule = decision.rule
        items.append({
            "subject_key": subject, "scope": "holding", "profile_id": uuid.UUID(pid), "instrument_id": uuid.UUID(iid),
            "symbol": row["symbol"], "name": row.get("name"), "asset_type": row["asset_type"], "action": rep["action"], "horizon": rule.horizon,
            "intent": intent, "confidence": rep["confidence"], "actionable": rule.actionable and rep["action"] != "HOLD", "urgent": rep["urgent"],
            "priority": priority.score(rule.severity, rep["urgent"], rep["confidence"], rep["impact"], rule.actionable and rep["action"] != "HOLD"),
            "rupee_impact": Decimal(str(round(rep["impact"], 2))), "headline": rep["headline"], "short_report": rep["short"],
            "full_report": rep["full"], "input_hash": rep["input_hash"], "rulebook_version": book.version, "rule_id": rule.id,
            "price_at_call": Decimal(str(row["price"])) if row.get("price") is not None else None,
            "benchmark_at_call": Decimal(str(nifty)) if nifty else None,
        })
        verdicts.setdefault(pid, {})[iid] = rep["action"]

    # ---- portfolio-level findings + health per profile ----
    health_out: dict[str, Any] = {}
    for pid, profile in ({} if filtered else profiles).items():
        prow = [r for r in rows if r["profile_id"] == pid]
        psum = next((p for p in holdings_["profiles"] if p["profile_id"] == pid), None)
        if not prow or not psum:
            continue
        sectors: dict[str, float] = {}
        total = sum(r["market_value"] for r in prow)
        for r in prow:
            if r.get("sector"):
                sectors[r["sector"]] = sectors.get(r["sector"], 0) + r["market_value"]
        psum = {**psum, "by_sector": {k: {"value": v, "pct": v / total * 100} for k, v in sectors.items()}}
        health_out[pid] = {"name": profile.get("display_name"), **health.health_score(psum, prow, verdicts.get(pid, {}), profile)}
        for fnd in health.findings(profile, psum, prow):
            conf = fnd["confidence"]
            fnd["headline"] = f"{profile.get('display_name')}: {fnd['headline']}"
            short = {"action": fnd["action"], "horizon": "long_term", "intent": None, "confidence": conf, "headline": fnd["headline"],
                     "reasons": fnd["reasons"],
                     "do": fnd.get("do") or fnd["headline"].split(": ", 1)[-1], "tax_impact": "Prefer redirecting new SIPs/contributions first — rebalancing via fresh money is tax-free.",
                     "what_would_change": fnd["what_would_change"], "suggestion": {"type": "rebalance", "value": round(fnd["impact"], 2)},
                     "rupee_impact": round(fnd["impact"], 2), "urgent": False, "data_quality": {"grade": "A"}, "composite_score": None}
            full = {"summary": {**short, "profile": profile.get("display_name")}, "evidence": fnd["evidence"],
                    "decision_trace": {"rulebook_version": book.version, "matched_rule": fnd["rule_id"], "facts": fnd["evidence"]},
                    "risks_and_counter_arguments": ["Rebalancing by selling can trigger capital-gains tax and exit loads."],
                    "history": [], "disclaimer": report.DISCLAIMER}
            full["input_hash"] = report.input_hash({"k": str(fnd["evidence"])}, book.version)
            items.append({
                "subject_key": f"portfolio:{pid}:{fnd['rule_id']}", "scope": "profile", "profile_id": uuid.UUID(pid), "instrument_id": None,
                "symbol": None, "name": profile.get("display_name"), "asset_type": None, "action": fnd["action"], "horizon": "long_term",
                "intent": None, "confidence": conf, "actionable": True, "urgent": False,
                "priority": priority.score(fnd["severity"], False, conf, fnd["impact"], True), "rupee_impact": Decimal(str(round(fnd["impact"], 2))),
                "headline": fnd["headline"], "short_report": short, "full_report": full, "input_hash": full["input_hash"],
                "rulebook_version": book.version, "rule_id": fnd["rule_id"], "price_at_call": None, "benchmark_at_call": None,
            })

    priority.bucketise(items, settings.weekly_action_budget)
    stats = {"new": 0, "updated": 0, "unchanged": 0}
    touched: list[Recommendation] = []
    seen_subjects: set[str] = set()
    for item in items:
        rec, what = await _persist(db, household_id, run.id, item)
        stats[what] += 1
        touched.append(rec)
        seen_subjects.add(item["subject_key"])
    # holdings that no longer exist / findings that resolved -> close out
    stale = (await db.execute(select(Recommendation).where(Recommendation.household_id == household_id, Recommendation.status.in_(["open", "snoozed"])))).scalars()
    resolved = 0
    for r in stale:
        in_scope = (profile_id is None or r.profile_id == profile_id) and wanted(r.asset_type, r.instrument_id)
        if in_scope and r.subject_key not in seen_subjects:
            r.status, r.change_reason = "superseded", "Resolved: condition no longer present or holding exited"
            resolved += 1
    stats["resolved"] = resolved

    total_value = sum(per_profile_value.values())
    family = None
    if filtered:  # a partial run keeps the last full run's health scores
        prev = await db.scalar(select(AdvisorRun).where(AdvisorRun.household_id == household_id, AdvisorRun.status == "done",
                                                        AdvisorRun.id != run.id).order_by(AdvisorRun.started_at.desc()).limit(1))
        health_out, family = dict((prev.stats or {}).get("health") or {}) if prev else {}, (prev.stats or {}).get("family_health") if prev else None
    elif health_out and total_value:
        family = round(sum(h["score"] * per_profile_value.get(pid, 0) for pid, h in health_out.items() if h.get("score") is not None) / total_value, 1)
    run.status, run.finished_at = "done", datetime.now(UTC)
    run.regime = regime
    counts: dict[str, int] = {}
    for i in items:
        counts[i["action"]] = counts.get(i["action"], 0) + 1
    run.stats = {**stats, "holdings": len(rows), "skipped_unpriced": skipped_unpriced[:20], "items": len(items), "actions": counts, "health": health_out, "family_health": family,
                 "focus": sum(1 for i in items if i["bucket"] == "focus"), "urgent": sum(1 for i in items if i["bucket"] == "urgent")}
    await db.commit()

    svc_token = create_service_token("advisor", household_id)  # service identity => stored as intent_source='auto'
    for a in auto_intents:  # remember auto-classified intents (never overrides a user's choice)
        try:
            await http.call(common.portfolio_url, "PUT", "/api/v1/holding-prefs", token=svc_token, json=a)
        except Exception:
            pass
    log.info("advisor.run_done", run_id=str(run.id), **stats)
    await publish("advisor.run_done", {"run_id": str(run.id), "scope": run.scope, "trigger": trigger}, household_id=str(household_id))
    return {"run_id": str(run.id), "rulebook_version": book.version, **run.stats, "regime": regime.get("regime"),
            "scope": run.scope, "to_review": review_queue(touched)}


def review_queue(recs: list[Recommendation]) -> list[str]:
    """Which calls to (re)send for an AI check: never checked, or the last check failed / had no AI — trades first,
    then holds, highest priority first. Already-reviewed calls on unchanged inputs are skipped by the reviewer."""
    todo = [r for r in recs if r.scope == "holding" and (r.ai_consensus in ("pending", "ai_failed", "rules_only")
                                                         or (r.short_report or {}).get("ai_stale"))]  # reviewed on older inputs
    return [str(r.id) for r in sorted(todo, key=lambda r: (not r.actionable, -(r.priority or 0)))]
