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
from fm_common.redis import get_redis

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
        if item["scope"] != "holding":
            prev.ai_consensus = "rules_only"
        return prev, "updated" if changed else "unchanged"
    rec = Recommendation(id=uuid.uuid4(), household_id=household_id, run_id=run_id, subject_key=item["subject_key"], status="open", **fields)
    if item["scope"] != "holding":  # family-level findings are rule calls on the whole portfolio; only holdings go to the AI reviewers
        rec.ai_consensus = "rules_only"
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


RUN_LOCK_SECONDS = 900   # a run never takes this long; the lock expires on its own if a worker dies mid-run
RUN_WAIT_SECONDS = 600   # how long a second run waits for the first one to finish


class RunBusy(RuntimeError):
    pass


async def run_household(db: AsyncSession, household_id: uuid.UUID, token: str, trigger: str = "manual", profile_id: uuid.UUID | None = None,
                        asset_types: list[str] | None = None, other_assets: bool = False,
                        instrument_ids: list[uuid.UUID] | None = None) -> dict[str, Any]:
    """Analyse a household, one person (``profile_id``), some kinds of holding (``asset_types``, or
    ``other_assets`` = everything except stocks, ETFs and mutual funds) or exactly some holdings
    (``instrument_ids`` — "analyse this one stock").

    One run at a time per household: two at once (an automatic run after an import + "Run analysis") would each
    create a card for the same holding. A second run waits for the first, then runs on the fresh state."""
    lock = get_redis().lock(f"fm:advisor:run-lock:{household_id}", timeout=RUN_LOCK_SECONDS, blocking_timeout=RUN_WAIT_SECONDS)
    if not await lock.acquire():
        raise RunBusy("Another analysis of this family is still running — try again in a few minutes.")
    try:
        await dedupe_open(db, household_id)
        return await _run_household(db, household_id, token, trigger, profile_id, asset_types, other_assets, instrument_ids)
    finally:
        try:
            await lock.release()
        except Exception:  # noqa: BLE001 — expired already; nothing to release
            pass


async def dedupe_open(db: AsyncSession, household_id: uuid.UUID) -> int:
    """Cards left twice by runs that overlapped before the lock existed: keep the newest per holding / finding."""
    rows = (await db.execute(select(Recommendation).where(Recommendation.household_id == household_id, Recommendation.status != "superseded")
                             .order_by(Recommendation.subject_key, Recommendation.created_at.desc()))).scalars()
    seen: set[str] = set()
    n = 0
    for r in rows:
        if r.subject_key in seen:
            r.status = "superseded"
            n += 1
        seen.add(r.subject_key)
    if n:
        await db.commit()
        log.info("advisor.duplicates_superseded", household_id=str(household_id), n=n)
    return n


async def _run_household(db: AsyncSession, household_id: uuid.UUID, token: str, trigger: str, profile_id: uuid.UUID | None,
                         asset_types: list[str] | None, other_assets: bool, instrument_ids: list[uuid.UUID] | None) -> dict[str, Any]:
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
    fund_facts: dict[str, dict[str, dict[str, Any]]] = {}  # profile → instrument → what an overlap needs to pick a fund
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
        if row["asset_type"] in ("mutual_fund", "etf"):
            mf = ev.get("fund") or {}
            cost = mf.get("cost") or {}
            meta = row.get("meta") or {}
            ff = fund_facts.setdefault(pid, {}).setdefault(iid, {
                "id": iid, "name": row.get("name") or row["symbol"], "value": 0.0, "price": row.get("price"),
                "lots": [], "score": mf.get("score") if mf.get("available") else None,
                "expense_ratio": cost.get("expense_ratio_pct", meta.get("expense_ratio")),
                "regular": cost.get("is_regular_plan", meta.get("plan") == "regular"), "category": meta.get("mf_category"),
                "verdict": rep["action"], "code": str(row["symbol"]), "asset_type": row["asset_type"]})
            ff["value"] += row["market_value"]  # the same fund in two of the person's portfolios
            ff["lots"] += row.get("lots") or []
            # the same SIP answer Holdings shows (purchase dates, or the person's own yes / no); unknown counts as no
            sip = row.get("sip") or {}
            ff["sip_active"] = bool(ff.get("sip_active")) or (bool(sip.get("active")) if "active" in sip else
                                                              health.sip_active(row.get("lots") or [], datetime.now(UTC).date(),
                                                                                estimated=bool(row.get("dates_estimated"))))

    # ---- portfolio-level findings + health per profile ----
    health_out: dict[str, Any] = {}
    looks = await _lookthrough_by_profile(rows, token)  # also on a "Mutual funds" run: overlap needs only the funds
    today = datetime.now(UTC).date()
    # one decision per fund, per person: one fund per role; a role whose best fund lags gets its replacement first,
    # then the steps are phased within what is left of the person's tax-free long-term gain (the Tax page's figure)
    plans = {pid: health.fund_lineup(ff, (looks.get(pid) or {}).get("overlap", []), today) for pid, ff in fund_facts.items()}
    await _retarget_lagging_roles(plans, token)
    ltcg_left = await _ltcg_headroom(token, today) if plans else {}
    for pid, plan in plans.items():
        health.phase(plan, fund_facts[pid], today, ltcg_left.get(pid, health.LTCG_EXEMPT))
    _apply_consolidation(items, plans, book.version, today)
    await _add_switch_targets(items, rows, token, book.version)
    for pid, profile in profiles.items():
        if filtered:  # a partial run: only the fund-consolidation cards (they need just the funds)
            for fnd in health.lookthrough_findings(profile, 1.0, {"stocks": [], "overlap": (looks.get(pid) or {}).get("overlap", [])},
                                                   fund_facts.get(pid), plan=plans.get(pid) or []):
                items.append(_finding_item(pid, profile, fnd, book.version))
            continue
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
        for fnd in health.findings(profile, psum, prow, looks.get(pid), fund_facts.get(pid), plan=plans.get(pid) or []):
            conf = fnd["confidence"]
            fnd["headline"] = f"{profile.get('display_name')}: {fnd['headline']}"
            short = {"action": fnd["action"], "horizon": "long_term", "intent": None, "confidence": conf, "headline": fnd["headline"],
                     "reasons": fnd["reasons"],
                     "do": fnd.get("do") or fnd["headline"].split(": ", 1)[-1], "tax_impact": "Prefer redirecting new SIPs/contributions first — rebalancing via fresh money is tax-free.",
                     "what_would_change": fnd["what_would_change"], "suggestion": {"type": "rebalance", "value": round(fnd["impact"], 2)},
                     **({"plan": fnd["plan"]} if fnd.get("plan") else {}), **({"lineup": fnd["lineup"]} if fnd.get("lineup") else {}),
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


def _finding_item(pid: str, profile: dict[str, Any], fnd: dict[str, Any], version: str) -> dict[str, Any]:
    """A portfolio-level finding as a recommendation row (used where the full health pass doesn't run)."""
    head = f"{profile.get('display_name')}: {fnd['headline']}"
    short = {"action": fnd["action"], "horizon": "long_term", "intent": None, "confidence": fnd["confidence"], "headline": head,
             "reasons": fnd["reasons"], "do": fnd.get("do") or fnd["headline"],
             "tax_impact": "Prefer redirecting new SIPs first — moving via fresh money is tax-free.", "what_would_change": fnd["what_would_change"],
             "suggestion": {"type": "rebalance", "value": round(fnd["impact"], 2)}, "rupee_impact": round(fnd["impact"], 2), "urgent": False,
             "data_quality": {"grade": "A"}, "composite_score": None, **({"plan": fnd["plan"]} if fnd.get("plan") else {}),
             **({"lineup": fnd["lineup"]} if fnd.get("lineup") else {})}
    full = {"summary": {**short, "profile": profile.get("display_name")}, "evidence": fnd["evidence"],
            "decision_trace": {"rulebook_version": version, "matched_rule": fnd["rule_id"], "facts": fnd["evidence"]},
            "risks_and_counter_arguments": ["Selling can trigger capital-gains tax and exit loads."], "history": [], "disclaimer": report.DISCLAIMER}
    full["input_hash"] = report.input_hash({"k": str(fnd["evidence"])}, version)
    return {"subject_key": f"portfolio:{pid}:{fnd['rule_id']}", "scope": "profile", "profile_id": uuid.UUID(pid), "instrument_id": None,
            "symbol": None, "name": profile.get("display_name"), "asset_type": None, "action": fnd["action"], "horizon": "long_term",
            "intent": None, "confidence": fnd["confidence"], "actionable": True, "urgent": False,
            "priority": priority.score(2, False, fnd["confidence"], fnd["impact"], True), "rupee_impact": Decimal(str(round(fnd["impact"], 2))),
            "headline": head, "short_report": short, "full_report": full, "input_hash": full["input_hash"], "rulebook_version": version,
            "rule_id": fnd["rule_id"], "price_at_call": None, "benchmark_at_call": None}


async def _add_switch_targets(items: list[dict[str, Any]], rows: list[dict[str, Any]], token: str, version: str) -> None:
    """A SWITCH on a lagging mutual fund names where to go: the best-scored funds in its own AMFI category right now
    (analytics ranks the category like your funds: rolling-return consistency, alpha, risk-adjusted return, cost).
    A better fund you already hold in that category comes first — consolidate rather than add another fund."""
    held: dict[str, set[str]] = {}
    for r in rows:
        if r["asset_type"] == "mutual_fund":
            held.setdefault(r["profile_id"], set()).add(str(r["symbol"]))
    todo = [i for i in items if i["action"] == "SWITCH" and i.get("asset_type") == "mutual_fund" and str(i.get("symbol") or "").isdigit()
            and not (i["short_report"] or {}).get("switch_into")]
    for it in todo[:8]:
        try:
            res = await http.get(common.analytics_url, f"/api/v1/analysis/mf/peers/{it['symbol']}", token=token, params={"limit": 10},
                                 request_timeout=240)
        except Exception as exc:
            log.warning("advisor.switch_targets_failed", symbol=it["symbol"], error=str(exc)[:200])
            continue
        add_switch_targets(it, res, held.get(str(it["profile_id"]), set()), version)


def add_switch_targets(it: dict[str, Any], res: dict[str, Any], held: set[str], version: str) -> None:
    mine = (res.get("self") or {}).get("score")
    better = [r for r in res.get("ranking") or [] if mine is None or r["score"] >= mine + 5]
    if not better:
        return
    owned = [r for r in better[:8] if r["code"] in held]
    picks = (owned[:1] + [r for r in better if r not in owned[:1]])[:3]
    label = res.get("label") or "same-category"
    sr = it["short_report"]

    def line(i: int, p: dict[str, Any]) -> str:
        bits = [f"fund score {p['score']:.0f}/100"]
        if p.get("consistency_pct") is not None:
            bits.append(f"beat its benchmark in {p['consistency_pct']:.0f}% of rolling {p.get('rolling_years') or 3}-yr periods")
        if p.get("return_3y_pct") is not None:
            bits.append(f"{p['return_3y_pct']:.1f}%/yr average 3-yr return")
        return f"{i}) {p['name']}{' — you already hold it' if p['code'] in held else ''} ({', '.join(bits)})"

    rank = f"ranks {res['self_rank']} of {res['of']}" if res.get("self_rank") else "is not among the ranked"
    sr["switch_candidates"] = [{**p, "held": p["code"] in held} for p in picks]
    sr["switch_category"] = label
    sr["do_prefix"] = f"Switch into one of the best {label} funds right now: " + "; ".join(line(i + 1, p) for i, p in enumerate(picks)) + "."
    sr["do"] = f"{sr['do_prefix']} {sr.get('do') or ''}".strip()
    sr["reasons"] = [f"{it.get('name') or it['symbol']} {rank} {label} funds by fund score{f' ({mine:.0f}/100)' if mine is not None else ''}",
                     *(sr.get("reasons") or [])][:5]
    it["input_hash"] = report.input_hash({"base": it["input_hash"], "targets": ",".join(p["code"] for p in picks)}, version)


def fy_of(d: date) -> str:
    y = d.year if d.month >= 4 else d.year - 1
    return f"{y}-{str(y + 1)[2:]}"


async def _ltcg_headroom(token: str, today: date) -> dict[str, float]:
    """Per person: this financial year's ₹1.25 lakh tax-free long-term gain still unused — gains already realised
    and gain-harvesting sales marked done count against it (the same figure the Tax page starts from)."""
    try:
        res = await http.get(common.portfolio_url, "/api/v1/tax/summary", token=token,
                             params={"scope": "household", "fy": fy_of(today), "with_plan": "false"}, request_timeout=60)
    except Exception as exc:
        log.warning("advisor.ltcg_headroom_failed", error=str(exc)[:200])
        return {}
    out = {}
    for p in (res or {}).get("people") or []:
        done = sum(float(d.get("booked") or 0) for d in p.get("harvest_done") or [] if d.get("kind") == "gain" and not d.get("reflected"))
        out[str(p["profile_id"])] = max(0.0, float((p.get("ltcg_exemption") or {}).get("left", health.LTCG_EXEMPT)) - done)
    return out


async def _retarget_lagging_roles(plans: dict[str, list[dict[str, Any]]], token: str) -> None:
    """A role where even the best fund you hold lags (``replace``): the target becomes the best fund of that category
    in the market right now (analytics ranking) — clearly better than the one held, never a fund that lags too."""
    for plan in plans.values():
        for g in plan:
            g.pop("into_name", None)
            if not g.get("replace"):
                continue
            try:
                res = await http.get(common.analytics_url, f"/api/v1/analysis/mf/peers/{g['keep_code']}", token=token, params={"limit": 3},
                                     request_timeout=240) if str(g.get("keep_code") or "").isdigit() else None
            except Exception as exc:
                log.warning("advisor.retarget_failed", fund=g["keep"], error=str(exc)[:200])
                res = None
            mine = ((res or {}).get("self") or {}).get("score")
            best = next((r for r in (res or {}).get("ranking") or [] if r.get("code") != str(g.get("keep_code"))
                         and (mine is None or r["score"] >= mine + 5)), None)
            if best:
                g["into_name"], g["into_code"] = best["name"], best["code"]
                g["into_detail"] = best


def _plan_tax(m: dict[str, Any], today: date) -> dict[str, Any]:
    """The long-term gain this sale books in the current financial year — the Tax page takes it off the
    ₹1.25 lakh tax-free allowance before suggesting any gain harvesting (one allowance, counted once)."""
    return {"plan_tax": {"fy": fy_of(today), "ltcg": round(float(m.get("fy_ltcg") or 0), 2)}} if m.get("mode") == "exit" else {}


def _apply_consolidation(items: list[dict[str, Any]], plans: dict[str, list[dict[str, Any]]], version: str,
                         today: date | None = None) -> None:
    """Each fund to move out becomes a SWITCH on its own card (into the fund kept for its role / overlap group, or —
    when every fund you hold there lags — the best fund of that category now), with the tax-aware steps. The kept fund
    says what moves into it. One role per fund, from ``health.fund_plan``."""
    today = today or datetime.now(UTC).date()
    by_subject = {i["subject_key"]: i for i in items}
    for pid, plan in plans.items():
        for g in plan:
            into = health.into_of(g)
            external = bool(g.get("replace"))
            for m in g["moves"]:
                it = by_subject.get(f"holding:{pid}:{m['id']}")
                if not it:
                    continue
                why = m.get("why_keep") or g["keep_reason"]
                if m.get("overlap_pct"):
                    first = (f"{m['overlap_pct']:.0f}% of its stocks are the same as {g['keep']}'s"
                             + (f" ({m.get('common_count')} in common: {', '.join(m['common'][:4])})" if m.get("common") else ""))
                else:
                    first = f"Same job in your portfolio as {g['keep']} ({g.get('role')}) — one fund per role is enough"
                why_line = ((f"Every {g.get('role')} fund you hold lags; {into} is the best-scored {g.get('role')} fund right now"
                             if g.get("into_name") else f"Every {g.get('role')} fund you hold lags — {g['keep']} is being replaced too (see its card)")
                            if external else f"Keep {g['keep']}: {why}")
                sr = it["short_report"]
                if m.get("mode") == "freeze":  # a decent fund, just redundant: hold it, only new money moves
                    head = (f"Hold {m['name']} — move its SIP to {into}" if m.get("sip") else f"Hold {m['name']} — nothing to do; new money goes to {into}")
                    sr.update({"decided_by": "fund_plan", "action": "HOLD", "verdict": "Hold · move SIP" if m.get("sip") else "Hold", "conviction": "high", "leaning": None,
                               "headline": head, "do": m["do"],
                               "reasons": [first, why_line, "Selling a decent fund only creates tax — redirecting new money does the job."][:5],
                               "what_would_change": f"Sell only if you need the money or {m['name']} starts to lag.",
                               "sip_into": {"name": into, "code": g.get("into_code") if external else g.get("keep_code"),
                                            "active_sip": bool(m.get("sip"))}})
                    it.update({"action": "HOLD", "headline": head, "actionable": True,
                               "input_hash": report.input_hash({"base": it["input_hash"], "freeze": into}, version)})
                    it["full_report"].setdefault("summary", {}).update({k: sr[k] for k in ("action", "headline", "do", "reasons")})
                    continue
                unnamed = external and not g.get("into_name")  # no better fund found yet: the card lists candidates instead
                head = f"Switch {m['name']}" if unnamed else f"Switch {m['name']} → {into}"
                do = m["do"]
                sr.update({"decided_by": "fund_plan", "action": "SWITCH", "rule_action": "SWITCH", "verdict": "Switch", "conviction": "high", "leaning": None,
                           "headline": head, "do": do, "reasons": [first, why_line, *m["costs"]][:5],
                           "what_would_change": f"Stay only if {m['name']} is the fund you want for {g.get('role') or 'this role'}.",
                           **({} if unnamed else {"switch_into": {"id": None if external else g["keep_id"], "name": into,
                                                                  "code": g.get("into_code") if external else g.get("keep_code")}}),
                           **_plan_tax(m, today)})
                it.update({"action": "SWITCH", "headline": head, "actionable": True, "confidence": max(it.get("confidence") or 0, 0.8),
                           "priority": priority.score(2, False, 0.8, m["move_value"], True),
                           "rupee_impact": Decimal(str(round(m["move_value"], 2))),
                           "input_hash": report.input_hash({"base": it["input_hash"], "plan": f"{into}:{m.get('overlap_pct')}"}, version)})
                it["full_report"].setdefault("summary", {}).update({k: sr[k] for k in ("action", "headline", "do", "reasons")})
            it = by_subject.get(f"holding:{pid}:{g['keep_id']}")
            if not it:
                continue
            sr = it["short_report"]
            if external:  # even the best fund you hold for this role lags: it is replaced too, in tax-aware steps
                km = g.get("keep_move") or {}
                head = f"Switch {g['keep']} → {into}" if g.get("into_name") else f"Switch {g['keep']}"
                sr.update({"decided_by": "fund_plan", "action": "SWITCH", "rule_action": "SWITCH", "verdict": "Switch", "conviction": "high", "leaning": None,
                           "headline": head, "do": km.get("do") or sr.get("do"),
                           "reasons": [f"It's the best {g.get('role')} fund you hold, but it lags"
                                       + (f" — {into} is the best-scored {g.get('role')} fund right now" if g.get("into_name") else ""),
                                       *km.get("costs", []), *(sr.get("reasons") or [])][:5],
                           **({"switch_into": {"id": None, "name": into, "code": g.get("into_code")}} if g.get("into_name") else {}),
                           **_plan_tax(km, today)})
                it.update({"action": "SWITCH", "headline": head, "actionable": True})
            elif it["action"] in ("HOLD", "ADD", "ACCUMULATE"):  # the fund to keep: the one place new money for this role goes
                names = ", ".join(m["name"] for m in g["moves"][:3]) + (" …" if len(g["moves"]) > 3 else "")
                role = f" for {g['role']}" if g.get("role") else ""
                sr.update({"decided_by": "fund_plan", "action": "ADD", "verdict": "Keep · invest here", "leaning": None,
                           "do": (f"Keep {g['keep']} — it's your fund{role}: new money (SIPs, lump sums) for this role goes here. "
                                  + (f"Move {names} into it over time (see their cards for the tax-friendly steps)." if g["moves"] else "")).strip(),
                           "reasons": [(f"Best of the {len(g['moves']) + 1} {g.get('role') or 'overlapping'} funds you hold: {g['keep_reason']}"
                                        if g["moves"] else f"Your only {g.get('role') or ''} fund, and it doesn't lag").replace("  ", " "),
                                       *(sr.get("reasons") or [])][:5],
                           **({"consolidate_from": [m["name"] for m in g["moves"]]} if g["moves"] else {})})
                head = (f"Keep {g['keep']} — move {len(g['moves'])} fund{'s' if len(g['moves']) > 1 else ''} into it" if g["moves"]
                        else f"Keep {g['keep']} — invest here{role}")
                it.update({"action": "ADD", "headline": head, "actionable": True})
                sr["headline"] = head
            else:
                continue
            it["full_report"].setdefault("summary", {}).update({k: sr[k] for k in ("action", "headline", "do", "reasons") if k in sr})
            it["input_hash"] = report.input_hash({"base": it["input_hash"], "keep": g["keep_id"], "into": into}, version)


async def _lookthrough_by_profile(rows: list[dict[str, Any]], token: str) -> dict[str, dict[str, Any]]:
    """Per profile: true stock exposure through funds + fund overlap (E16), from market-svc's fund portfolios."""
    from fm_common.lookthrough import combine, overlap

    fund_ids = sorted({str(r["instrument_id"]) for r in rows if r["asset_type"] in ("mutual_fund", "etf") and r["market_value"] > 0})
    if not fund_ids:
        return {}
    stock_ids = sorted({str(r["instrument_id"]) for r in rows if r["asset_type"] == "stock"})
    try:
        funds = await http.post(common.market_url, "/api/v1/market/funds/lookthrough", token=token, json={"ids": fund_ids})
        insts = await http.post(common.market_url, "/api/v1/market/instruments/batch", token=token, json={"ids": stock_ids}) if stock_ids else []
    except Exception as exc:
        log.warning("advisor.lookthrough_failed", error=str(exc)[:200])
        return {}
    isin_of = {i["id"]: i["isin"] for i in insts if i.get("isin")}
    out: dict[str, dict[str, Any]] = {}
    for pid in {r["profile_id"] for r in rows}:
        merged: dict[str, dict[str, Any]] = {}
        for r in rows:
            if r["profile_id"] != pid:
                continue
            m = merged.setdefault(str(r["instrument_id"]), {"instrument_id": str(r["instrument_id"]), "name": r.get("name") or r["symbol"],
                                                             "asset_type": r["asset_type"], "value": 0.0})
            m["value"] += r["market_value"]
        look = combine(list(merged.values()), funds, isin_of)
        look["overlap"] = overlap([{"name": m["name"], "id": k, "holdings": (funds.get(k) or {}).get("holdings") or []}
                                   for k, m in merged.items() if (funds.get(k) or {}).get("available")], min_pct=50)
        out[pid] = look
    return out


def review_queue(recs: list[Recommendation]) -> list[str]:
    """Which calls to (re)send for an AI check: never checked, or the last check failed / had no AI — trades first,
    then holds, highest priority first. Already-reviewed calls on unchanged inputs are skipped by the reviewer."""
    todo = [r for r in recs if r.scope == "holding" and (r.ai_consensus in ("pending", "ai_failed", "rules_only")
                                                         or (r.short_report or {}).get("ai_stale"))]  # reviewed on older inputs
    return [str(r.id) for r in sorted(todo, key=lambda r: (not r.actionable, -(r.priority or 0)))]
