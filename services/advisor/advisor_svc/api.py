from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import defer

from fm_common import http
from fm_common.config import settings as common
from fm_common.crypto import decrypt, encrypt, mask_tail
from fm_common.deps import DB, CurrentUser, Principal, require
from fm_common.identity.rbac import P_ADVISOR_ACT, P_ADVISOR_READ, P_ADVISOR_RUN, P_AI_CONFIGURE, P_INTERNAL

from . import engine, queue, rules, shadow
from .ai import narrate
from .ai import review as ai_review
from .ai.registry import SUPPORTED, _env_defaults, clean, household_clients, make_client, paused, test_client, unpause
from .config import settings as adv_settings
from .models import AdvisorRun, AIProvider, AIReview, Recommendation, ShadowTrade

router = APIRouter(prefix="/advisor", tags=["advisor"])


# Lists need only the person's name from the full report — not the whole report (large JSON per card, which made
# /summary and /recommendations spend seconds decoding).
PROFILE_NAME = Recommendation.full_report["summary"]["profile"].astext.label("profile_name")


async def light_cards(db: Any, stmt: Any) -> list[tuple[Recommendation, str | None]]:
    """Run a ``select(Recommendation)`` without loading ``full_report`` → (recommendation, profile name) pairs."""
    return [(r, name) for r, name in (await db.execute(stmt.add_columns(PROFILE_NAME).options(defer(Recommendation.full_report)))).all()]


def card(r: Recommendation, ai_error: str | None = None, profile_name: Any = ...) -> dict[str, Any]:
    return {
        "ai_error": ai_error,
        "id": str(r.id), "scope": r.scope, "profile_id": str(r.profile_id) if r.profile_id else None,
        "instrument_id": str(r.instrument_id) if r.instrument_id else None, "symbol": r.symbol, "name": r.name, "asset_type": r.asset_type,
        "action": r.action, "horizon": r.horizon, "intent": r.intent, "confidence": r.confidence, "priority": r.priority, "bucket": r.bucket,
        "actionable": r.actionable, "rupee_impact": float(r.rupee_impact) if r.rupee_impact is not None else None, "headline": r.headline,
        "short_report": r.short_report, "narrative": r.narrative, "ai_consensus": r.ai_consensus, "status": r.status,
        "snooze_until": r.snooze_until, "change_reason": r.change_reason, "rule_id": r.rule_id, "rulebook_version": r.rulebook_version,
        "created_at": r.created_at, "updated_at": r.updated_at,
        "profile_name": ((r.full_report or {}).get("summary") or {}).get("profile") if profile_name is ... else profile_name,
    }


class RunIn(BaseModel):
    profile_id: uuid.UUID | None = None
    asset_types: list[Literal["stock", "etf", "mutual_fund", "nps", "epf", "ppf", "fd", "bond", "gold", "reit"]] | None = None
    other_assets: bool = False  # everything except stocks, ETFs and mutual funds (NPS / EPF / PPF / …)
    instrument_ids: list[uuid.UUID] | None = Field(default=None, max_length=200)  # exactly these holdings ("analyse this one")


@router.post("/run")
async def run(body: RunIn, db: DB, principal: Principal = require(P_ADVISOR_RUN)) -> dict[str, Any]:
    try:
        result = await engine.run_household(db, principal.household_id, principal.token, "manual", body.profile_id,
                                            body.asset_types, body.other_assets, body.instrument_ids)
    except engine.RunBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        await engine.mark_failed(principal.household_id, exc)
        raise
    if result["to_review"]:
        await queue.enqueue("review_and_narrate", str(principal.household_id), result["to_review"], job_id=f"review:{result['run_id']}")
    return result


@router.get("/summary")
async def summary(db: DB, principal: Principal = require(P_ADVISOR_READ), profile_ids: str | None = None) -> dict[str, Any]:
    """``profile_ids`` (comma-separated): only these people's actions — the dashboard's person / group view."""
    wanted = [uuid.UUID(x) for x in (profile_ids or "").split(",") if x.strip()]
    last = (await db.execute(select(AdvisorRun).where(AdvisorRun.household_id == principal.household_id, AdvisorRun.status == "done")
                             .order_by(AdvisorRun.started_at.desc()).limit(1))).scalar_one_or_none()
    top_q = select(Recommendation).where(
        Recommendation.household_id == principal.household_id, Recommendation.status == "open", Recommendation.bucket.in_(["urgent", "focus"]))
    if wanted:
        top_q = top_q.where(Recommendation.profile_id.in_(wanted))
    top = await light_cards(db, top_q.order_by(Recommendation.priority.desc()).limit(7))
    latest = (await db.execute(select(AdvisorRun).where(AdvisorRun.household_id == principal.household_id)
                               .order_by(AdvisorRun.started_at.desc()).limit(1))).scalar_one_or_none()
    reviewers, primary = await household_clients(db, principal.household_id)
    return {
        "last_run": {"id": str(last.id), "at": last.finished_at, "rulebook_version": last.rulebook_version, "stats": last.stats, "regime": last.regime} if last else None,
        # the most recent attempt, when it was skipped (e.g. prices unavailable) — the UI explains why
        "last_attempt": {"at": latest.finished_at, "status": latest.status, "reason": (latest.stats or {}).get("skipped_reason")}
                        if latest and latest.status == "skipped" and (not last or latest.started_at > last.started_at) else None,
        "top_actions": [card(r, profile_name=name) for r, name in top],
        "ai": {"reviewers": [f"{c.provider}:{c.model}" for c in reviewers[:1]], "order": [f"{c.provider}:{c.model}" for c in reviewers],
               "primary": f"{primary.provider}:{primary.model}" if primary else None,
               "mode": "single_ai" if reviewers else "rules_only"},
        "schedule": schedule_info(latest),
        "ai_status": await ai_status(db, principal.household_id, wanted or None),
    }


def schedule_info(latest: AdvisorRun | None) -> dict[str, Any]:
    """When the advisor runs by itself — shown on the Advisor page so nobody has to guess."""
    now = datetime.now(UTC)
    nxt = now.replace(hour=11, minute=30, second=0, microsecond=0)  # 17:00 IST, after market close (worker cron)
    if nxt <= now:
        nxt += timedelta(days=1)
    return {
        "daily": "every day at 5:00 pm IST (after market close)", "next_daily_run": nxt.isoformat(),
        "also": "about 20 seconds after you import a file, add or change transactions, or a holding's price moves sharply",
        "latest": {"status": latest.status, "trigger": latest.trigger, "started_at": latest.started_at, "finished_at": latest.finished_at,
                   "error": (latest.stats or {}).get("error") or (latest.stats or {}).get("skipped_reason")} if latest else None,
    }


@router.get("/runs")
async def runs(db: DB, principal: Principal = require(P_ADVISOR_READ), limit: int = 15) -> list[dict[str, Any]]:
    """Recent advisor runs — when, why (daily / import / price move / you), and how it went."""
    rows = (await db.execute(select(AdvisorRun).where(AdvisorRun.household_id == principal.household_id)
                             .order_by(AdvisorRun.started_at.desc()).limit(min(max(limit, 1), 50)))).scalars()
    out = []
    for r in rows:
        st = r.stats or {}
        status = r.status
        if status == "running" and r.started_at and datetime.now(UTC) - r.started_at > timedelta(minutes=20):
            status = "failed"  # never finished: the worker died or timed out
        out.append({"id": str(r.id), "trigger": r.trigger, "scope": r.scope, "status": status, "started_at": r.started_at,
                    "finished_at": r.finished_at, "holdings": st.get("holdings"), "new": st.get("new"), "updated": st.get("updated"),
                    "error": st.get("error") or st.get("skipped_reason") or ("didn't finish (worker stopped or timed out)" if status == "failed" and not st.get("error") else None)})
    return out


@router.get("/recommendations")
async def list_recs(
    db: DB, principal: Principal = require(P_ADVISOR_READ), status: str = "open", bucket: str | None = None,
    profile_id: uuid.UUID | None = None, include_hold: bool = False, instrument_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    stmt = select(Recommendation).where(Recommendation.household_id == principal.household_id)
    if status != "all":
        stmt = stmt.where(Recommendation.status.in_(status.split(",")))
    if bucket:
        stmt = stmt.where(Recommendation.bucket.in_(bucket.split(",")))
    if profile_id:
        stmt = stmt.where(Recommendation.profile_id == profile_id)
    if instrument_id:
        stmt = stmt.where(Recommendation.instrument_id == instrument_id)
    if not include_hold:
        stmt = stmt.where(Recommendation.actionable.is_(True))
    rows = await light_cards(db, stmt.order_by(Recommendation.priority.desc(), Recommendation.updated_at.desc()).limit(500))
    errors = await ai_errors(db, [r for r, _ in rows if r.ai_consensus == "ai_failed"])
    return [card(r, errors.get(r.id), name) for r, name in rows]


@router.get("/fund-plan/tax")
async def fund_plan_tax(db: DB, principal: Principal = require(P_ADVISOR_READ), fy: str | None = None) -> list[dict[str, Any]]:
    """Long-term gains the fund plan books this financial year, per person and fund — the Tax page takes them off the
    ₹1.25 lakh tax-free allowance before suggesting any gain harvesting (one allowance, counted once)."""
    rows = (await db.execute(select(Recommendation).where(
        Recommendation.household_id == principal.household_id, Recommendation.status == "open", Recommendation.scope == "holding",
        Recommendation.action.in_(["SWITCH", "EXIT"])))).scalars()
    out = []
    for r in rows:
        pt = (r.short_report or {}).get("plan_tax") or {}
        if pt and (fy is None or pt.get("fy") == fy):
            out.append({"profile_id": str(r.profile_id), "instrument_id": str(r.instrument_id), "fy": pt.get("fy"), "ltcg": float(pt.get("ltcg") or 0)})
    return out


async def ai_errors(db: DB, recs: list[Recommendation]) -> dict[uuid.UUID, str]:
    """Why the AI check failed, per call — the latest failed attempt on the call's current inputs."""
    if not recs:
        return {}
    hashes = {r.id: r.input_hash for r in recs}
    out: dict[uuid.UUID, str] = {}
    for rv in (await db.execute(select(AIReview).where(AIReview.recommendation_id.in_(list(hashes)), AIReview.stance == "error")
                                .order_by(AIReview.created_at.desc()))).scalars():
        if rv.recommendation_id not in out and rv.input_hash == hashes[rv.recommendation_id]:
            detail = ((rv.issues or [{}])[0] or {}).get("detail") or "unknown error"
            out[rv.recommendation_id] = f"{rv.provider}: {detail}"[:300]
    return out


async def ai_status(db: DB, household_id: uuid.UUID, profile_ids: list[uuid.UUID] | None = None) -> dict[str, Any]:
    """How far the AI checks have got on the open calls: reviewed / failed (with the latest reason) / waiting / no AI."""
    q = select(Recommendation.ai_consensus, func.count()).where(
        Recommendation.household_id == household_id, Recommendation.status == "open", Recommendation.scope == "holding")
    if profile_ids:
        q = q.where(Recommendation.profile_id.in_(profile_ids))
    counts = {k: n for k, n in (await db.execute(q.group_by(Recommendation.ai_consensus))).all()}
    last_err = await db.scalar(select(AIReview).where(AIReview.household_id == household_id, AIReview.stance == "error")
                               .order_by(AIReview.created_at.desc()).limit(1))
    return {"reviewed": sum(counts.get(k, 0) for k in ("verified", "caution", "needs_review")), "failed": counts.get("ai_failed", 0),
            "waiting": counts.get("pending", 0), "no_ai": counts.get("rules_only", 0),
            "last_error": {"provider": last_err.provider, "detail": ((last_err.issues or [{}])[0] or {}).get("detail"), "at": last_err.created_at}
            if last_err else None}


class RecheckIn(BaseModel):
    profile_id: uuid.UUID | None = None
    asset_types: list[str] | None = None
    other_assets: bool = False
    rec_ids: list[uuid.UUID] | None = Field(default=None, max_length=200)  # exactly these calls (used by the readiness engine)


@router.post("/review-all")
async def review_all(body: RecheckIn, db: DB, principal: Principal = require(P_ADVISOR_RUN)) -> dict[str, Any]:
    """"Re-check with AI": queue every open call in the current view whose AI check is missing or failed."""
    q = select(Recommendation).where(Recommendation.household_id == principal.household_id, Recommendation.status == "open",
                                     Recommendation.scope == "holding")
    if body.profile_id:
        q = q.where(Recommendation.profile_id == body.profile_id)
    if body.rec_ids:
        q = q.where(Recommendation.id.in_(body.rec_ids))
    recs = [r for r in (await db.execute(q)).scalars()
            if not (body.asset_types or body.other_assets) or r.asset_type in (body.asset_types or [])
            or (body.other_assets and r.asset_type not in ("stock", "etf", "mutual_fund"))]
    ids = engine.review_queue(recs)
    if ids:
        await queue.enqueue("review_and_narrate", str(principal.household_id), ids, job_id=f"recheck:{principal.household_id}:{uuid.uuid4().hex[:8]}")
    return {"queued": len(ids), "per_run": adv_settings.ai_review_max_per_run}


# ============================== Internal (readiness engine) ==============================
@router.get("/internal/readiness-input")
async def readiness_input(db: DB, principal: Principal = require(P_INTERNAL)) -> dict[str, Any]:
    """For every open holding call of this household: what its report was built with, and whether the AI has
    reviewed its current inputs — the readiness engine compares this with the market data stored now."""
    recs = list((await db.execute(select(Recommendation).where(
        Recommendation.household_id == principal.household_id, Recommendation.status.in_(["open", "snoozed"]),
        Recommendation.scope == "holding"))).scalars())
    reviews: dict[uuid.UUID, list[AIReview]] = {}
    if recs:
        for rv in (await db.execute(select(AIReview).where(AIReview.recommendation_id.in_([r.id for r in recs]))
                                    .order_by(AIReview.created_at.desc()))).scalars():
            reviews.setdefault(rv.recommendation_id, []).append(rv)
    chain, _ = await household_clients(db, principal.household_id, include_paused=True)
    pauses = await paused(principal.household_id)
    out = []
    for r in recs:
        full = r.full_report or {}
        dq = full.get("data_quality") or {}
        src = dq.get("sources") or {}
        fund = (full.get("evidence") or {}).get("fundamental") or {}
        mine = [v for v in reviews.get(r.id, []) if v.input_hash == r.input_hash]
        latest = mine[0] if mine else None
        out.append({
            "rec_id": str(r.id), "profile_id": str(r.profile_id) if r.profile_id else None,
            "profile_name": (full.get("summary") or {}).get("profile"),
            "instrument_id": str(r.instrument_id) if r.instrument_id else None, "symbol": r.symbol, "name": r.name,
            "asset_type": r.asset_type, "action": r.action, "built_at": r.updated_at.isoformat() if r.updated_at else None,
            "report": {"grade": dq.get("grade"), "bars": (src.get("history") or {}).get("bars"),
                       "fundamentals_available": bool(fund.get("available")),
                       "fundamentals_as_of": (src.get("fundamentals") or {}).get("as_of")},
            "ai": {"reviewed": any(v.stance != "error" for v in mine), "consensus": r.ai_consensus,
                   "earlier_review_at": ((r.short_report or {}).get("ai_stale") or {}).get("reviewed_at"),
                   "last_error": ((latest.issues or [{}])[0] or {}).get("detail") if latest and latest.stance == "error" else None,
                   "last_attempt": latest.created_at.isoformat() if latest else None},
        })
    return {"holdings": out, "ai_providers": [c.provider for c in chain],
            "ai_paused": {p: v.get("reason") for p, v in pauses.items() if p in {c.provider for c in chain}}}


class AnalyseIn(BaseModel):
    instrument_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    profile_id: uuid.UUID | None = None
    reason: str = Field(default="readiness", max_length=40)


@router.post("/internal/analyse")
async def internal_analyse(body: AnalyseIn, principal: Principal = require(P_INTERNAL)) -> dict[str, Any]:
    """Queue a re-analysis of exactly these holdings (in the background)."""
    ids = sorted(str(i) for i in body.instrument_ids)
    # same holdings within the same minute → one job (arq refuses a duplicate id while it's queued or kept)
    minute = int(datetime.now(UTC).timestamp() // 60)
    what = uuid.uuid5(uuid.NAMESPACE_OID, f"{body.profile_id}:" + ",".join(ids)).hex[:12]
    job = f"analyse:{principal.household_id}:{what}:{minute}"
    await queue.enqueue("run_household_job", str(principal.household_id), f"event:{body.reason}"[:64],
                        str(body.profile_id) if body.profile_id else None, ids, job_id=job)
    return {"queued": len(ids), "job_id": job}


@router.get("/recommendations/{rec_id}")
async def get_rec(rec_id: uuid.UUID, db: DB, principal: Principal = require(P_ADVISOR_READ)) -> dict[str, Any]:
    r = await db.get(Recommendation, rec_id)
    if r is None or r.household_id != principal.household_id:
        raise HTTPException(404, "Recommendation not found")
    reviews = (await db.execute(select(AIReview).where(AIReview.recommendation_id == r.id).order_by(AIReview.created_at.desc()))).scalars().all()
    latest: dict[str, AIReview] = {}
    for rv in reviews:
        latest.setdefault(rv.provider, rv)
    return {
        **card(r), "full_report": r.full_report, "supersedes_id": str(r.supersedes_id) if r.supersedes_id else None,
        "user_note": r.user_note, "acted_at": r.acted_at,
        "ai_reviews": [{"provider": v.provider, "model": v.model, "stance": v.stance, "confidence": v.confidence, "issues": v.issues,
                        "rationale": v.rationale, "counter_case": v.counter_case, "latency_ms": v.latency_ms,
                        "suggested_action": v.suggested_action, "plain_verdict": v.plain_verdict, "horizon": v.horizon,
                        "opinion": (r.short_report or {}).get("rule_action") == "REVIEW",
                        "reviewed_at": v.created_at, "current": v.input_hash == r.input_hash} for v in latest.values()],
    }


class ActIn(BaseModel):
    action: Literal["accept", "snooze", "dismiss", "reopen"]
    snooze_days: int = Field(default=7, ge=1, le=180)
    note: str | None = Field(default=None, max_length=2000)


@router.post("/recommendations/{rec_id}/act")
async def act(rec_id: uuid.UUID, body: ActIn, db: DB, principal: Principal = require(P_ADVISOR_ACT)) -> dict[str, Any]:
    """P1: 'accept' records the decision and a *virtual* shadow trade. No order is placed anywhere."""
    r = await db.get(Recommendation, rec_id)
    if r is None or r.household_id != principal.household_id:
        raise HTTPException(404, "Recommendation not found")
    if r.status == "superseded":
        raise HTTPException(409, "This recommendation has been superseded by a newer analysis")
    r.acted_at, r.acted_by, r.user_note = datetime.now(UTC), principal.user_id, body.note
    if body.action == "accept":
        r.status = "accepted"
        if not (await db.scalar(select(ShadowTrade.id).where(ShadowTrade.recommendation_id == r.id))):
            if (t := shadow.shadow_trade_for(r)) is not None:
                db.add(t)
    elif body.action == "snooze":
        r.status, r.snooze_until = "snoozed", date.today() + timedelta(days=body.snooze_days)
    elif body.action == "dismiss":
        r.status = "dismissed"
    else:
        r.status, r.snooze_until = "open", None
    await db.commit()
    return card(r)


@router.post("/recommendations/{rec_id}/review")
async def request_review(rec_id: uuid.UUID, db: DB, principal: Principal = require(P_ADVISOR_READ)) -> dict[str, Any]:
    """Re-check one call with every enabled AI *now* and report each one's outcome (the old queued
    version reused a job id, so a repeat click was silently dropped by the queue)."""
    r = await db.get(Recommendation, rec_id)
    if r is None or r.household_id != principal.household_id:
        raise HTTPException(404, "Recommendation not found")
    res = await ai_review.review_recommendations(db, principal.household_id, [r.id], force=True)
    return {"status": "done" if res.get("results") else res.get("mode", "done"), **res}


@router.get("/shadow")
async def shadow_portfolio(db: DB, principal: Principal = require(P_ADVISOR_READ)) -> dict[str, Any]:
    trades = list((await db.execute(select(ShadowTrade).where(ShadowTrade.household_id == principal.household_id).order_by(ShadowTrade.created_at))).scalars())
    quotes = {}
    if trades:
        quotes = await http.get(common.market_url, "/api/v1/market/quotes", token=principal.token, params={"symbols": ",".join(sorted({t.symbol for t in trades}))})
    return shadow.shadow_summary(trades, quotes)


@router.get("/scorecard")
async def scorecard(db: DB, principal: Principal = require(P_ADVISOR_READ)) -> dict[str, Any]:
    return await shadow.scorecard(db, principal.household_id)


@router.get("/rulebook")
async def rulebook(principal: CurrentUser) -> dict[str, Any]:
    book = rules.load_rulebook()
    return {"version": book.version, "rules": [{"id": r.id, "action": r.action, "horizon": r.horizon, "reason": r.reason, "when": r.when,
                                                 "base_confidence": r.base_confidence, "urgent": r.urgent} for r in book.rules]}


# ---------------- AI providers (E24 configuration) ----------------
class ProviderIn(BaseModel):
    provider: Literal["claude", "openai", "gemini", "groq", "cloudflare", "ollama"]
    model: str = Field(max_length=80)
    api_key: str | None = Field(default=None, max_length=500)
    base_url: str | None = Field(default=None, max_length=255)
    is_enabled: bool = True
    is_primary: bool = False
    priority: int | None = Field(default=None, ge=1, le=999)
    monthly_budget_usd: float = Field(default=10, ge=0, le=1000)

    @field_validator("api_key", "base_url", mode="before")
    @classmethod
    def _no_whitespace(cls, v: object) -> object:  # pasted keys often carry tabs / newlines
        return clean(v) if isinstance(v, str) else v

    @field_validator("model", mode="before")
    @classmethod
    def _clean_model(cls, v: object) -> object:  # a pasted "\tgemini-3-flash" broke every request URL
        if isinstance(v, str) and not clean(v):
            raise ValueError("Enter a model name")
        return clean(v) if isinstance(v, str) else v


def provider_out(p: AIProvider, pauses: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    ps = (pauses or {}).get(p.provider)
    return {"provider": p.provider, "model": clean(p.model), "api_key_hint": p.api_key_hint, "base_url": p.base_url, "is_enabled": p.is_enabled,
            "is_primary": p.is_primary, "priority": p.priority, "monthly_budget_usd": p.monthly_budget_usd, "updated_at": p.updated_at,
            "paused_until": datetime.fromtimestamp(ps["until"], UTC).isoformat() if ps else None, "paused_reason": ps["reason"] if ps else None}


@router.get("/ai-providers")
async def list_providers(db: DB, principal: Principal = require(P_ADVISOR_READ)) -> dict[str, Any]:
    """Every provider in the exact order the advisor tries them (saved ones, then .env ones), then the switched-off ones."""
    rows = (await db.execute(select(AIProvider).where(AIProvider.household_id == principal.household_id)
                             .order_by(AIProvider.priority, AIProvider.provider))).scalars().all()
    chain, primary = await household_clients(db, principal.household_id)
    full_chain, _ = await household_clients(db, principal.household_id, include_paused=True)
    pauses = await paused(principal.household_id)
    env = {d["provider"]: d for d in _env_defaults() if make_client(d["provider"], d["model"], d["api_key"], d["base_url"]) is not None}
    configured = [{**provider_out(p, pauses), "from_env": p.api_key_encrypted is None and p.provider in env} for p in rows]
    have = {c["provider"] for c in configured}
    for name, d in env.items():  # set in .env (server-wide) but not saved here: still used, so show them
        if name in have:
            continue
        ps = pauses.get(name)
        configured.append({"provider": name, "model": clean(d["model"]), "api_key_hint": "from .env", "base_url": d["base_url"],
                           "is_enabled": True, "is_primary": False, "priority": 1000, "from_env": True,
                           "paused_until": datetime.fromtimestamp(ps["until"], UTC).isoformat() if ps else None,
                           "paused_reason": ps["reason"] if ps else None})
    pos = {c.provider: i for i, c in enumerate(full_chain)}
    configured.sort(key=lambda c: (pos.get(c["provider"], 10_000), c["provider"]))
    for c in configured:
        c["order"] = pos[c["provider"]] + 1 if c["provider"] in pos else None  # None = not used (switched off / incomplete)
    return {"supported": list(SUPPORTED), "configured": configured,
            "active": [{"provider": c.provider, "model": c.model} for c in chain], "primary": primary.provider if primary else None,
            "order_used": [f"{c.provider}:{c.model}" for c in chain], "paused": pauses}


class EnabledIn(BaseModel):
    enabled: bool


@router.put("/ai-providers/{provider}/enabled")
async def set_provider_enabled(provider: str, body: EnabledIn, db: DB, principal: Principal = require(P_AI_CONFIGURE)) -> dict[str, Any]:
    """Switch a provider off / on — including one that comes from .env (its key stays in .env; this household
    just stops using it)."""
    if provider not in SUPPORTED:
        raise HTTPException(404, "Unknown provider")
    p = await db.scalar(select(AIProvider).where(AIProvider.household_id == principal.household_id, AIProvider.provider == provider))
    if p is None:
        d = next(x for x in _env_defaults() if x["provider"] == provider)
        last = await db.scalar(select(func.max(AIProvider.priority)).where(AIProvider.household_id == principal.household_id, AIProvider.priority < 500))
        p = AIProvider(id=uuid.uuid4(), household_id=principal.household_id, provider=provider, model=clean(d["model"]) or provider,
                       priority=(last or 0) + 1)
        db.add(p)
    p.is_enabled = body.enabled
    await db.commit()
    return await list_providers(db, principal)


class OrderIn(BaseModel):
    providers: list[str] = Field(min_length=1, max_length=10)


@router.put("/ai-providers/order")
async def reorder_providers(body: OrderIn, db: DB, principal: Principal = require(P_AI_CONFIGURE)) -> dict[str, Any]:
    """Set the fallback order: the first is used, the next ones only if it fails or is out of quota."""
    rows = {p.provider: p for p in (await db.execute(select(AIProvider).where(AIProvider.household_id == principal.household_id))).scalars()}
    env = {d["provider"]: d for d in _env_defaults()}
    for name in body.providers:  # a provider from .env gets a row here to hold its place (its key stays in .env)
        if name not in rows and name in env and env[name]["model"]:
            rows[name] = AIProvider(id=uuid.uuid4(), household_id=principal.household_id, provider=name, model=env[name]["model"])
            db.add(rows[name])
    for i, name in enumerate(body.providers):
        if name in rows:
            rows[name].priority = i + 1
            rows[name].is_primary = i == 0
    await db.commit()
    return await list_providers(db, principal)


@router.delete("/ai-providers/{provider}/pause")
async def retry_provider(provider: str, principal: Principal = require(P_AI_CONFIGURE)) -> dict[str, Any]:
    """Use a paused provider again now (e.g. after upgrading its plan)."""
    await unpause(principal.household_id, provider)
    return {"provider": provider, "paused": False}


@router.put("/ai-providers")
async def upsert_provider(body: ProviderIn, db: DB, principal: Principal = require(P_AI_CONFIGURE)) -> dict[str, Any]:
    p = await db.scalar(select(AIProvider).where(AIProvider.household_id == principal.household_id, AIProvider.provider == body.provider))
    if p is None:
        last = await db.scalar(select(func.max(AIProvider.priority)).where(AIProvider.household_id == principal.household_id, AIProvider.priority < 500))
        p = AIProvider(id=uuid.uuid4(), household_id=principal.household_id, provider=body.provider, model=body.model,
                       priority=1 if body.is_primary else (last or 0) + 1)  # new providers join the end of the fallback order
        db.add(p)
    p.model, p.is_enabled, p.monthly_budget_usd = body.model, body.is_enabled, body.monthly_budget_usd
    p.base_url = body.base_url or p.base_url
    if body.priority is not None:
        p.priority = body.priority
    if body.api_key:
        p.api_key_encrypted = encrypt(body.api_key, aad=f"{principal.household_id}:{body.provider}")
        p.api_key_hint = mask_tail(body.api_key, 4)[-8:]
    if body.is_primary:  # "use first": move it to the top of the order
        for other in (await db.execute(select(AIProvider).where(AIProvider.household_id == principal.household_id))).scalars():
            other.is_primary = other.provider == body.provider
            if other.provider != body.provider and other.priority <= 1:
                other.priority = 2
        p.priority = 1
    p.is_primary = body.is_primary
    await db.commit()
    await unpause(principal.household_id, body.provider)  # new settings (e.g. a new key or plan): try it again
    client = make_client(body.provider, body.model, body.api_key or await _stored_key(p, principal), p.base_url)
    test = await test_client(client) if client else {"ok": False, "error": "Incomplete settings — an API key is required for this provider."}
    return {**provider_out(p), "test": test}


async def _stored_key(p: AIProvider | None, principal: Principal) -> str | None:
    if p is None or not p.api_key_encrypted:
        return None
    try:
        return decrypt(p.api_key_encrypted, aad=f"{principal.household_id}:{p.provider}")
    except Exception:
        return None


@router.post("/ai-providers/{provider}/test")
async def test_provider(provider: str, db: DB, principal: Principal = require(P_ADVISOR_READ)) -> dict[str, Any]:
    """One real request with the saved settings: HTTP status, latency and the provider's own error."""
    p = await db.scalar(select(AIProvider).where(AIProvider.household_id == principal.household_id, AIProvider.provider == provider))
    d = next((x for x in _env_defaults() if x["provider"] == provider), None)
    if d is None:
        raise HTTPException(404, "Unknown provider")
    client = make_client(provider, p.model if p else d["model"], await _stored_key(p, principal) or d["api_key"], (p.base_url if p else None) or d["base_url"])
    if client is None:
        return {"ok": False, "provider": provider, "error": "Not configured — add a model and API key first."}
    return await test_client(client)


@router.post("/ai-providers/test-all")
async def test_all_providers(db: DB, principal: Principal = require(P_ADVISOR_READ), review: bool = True) -> dict[str, Any]:
    """Settings → "Test all": every configured AI (saved or from .env, paused ones too), each with
    1) a tiny connectivity request and 2) a real review of one of your calls — the same size and format the
    advisor sends — so "works in a ping, fails on real reviews" (too large, rate limits, bad JSON) shows up here.
    A provider that passes both is un-paused."""
    import asyncio

    import orjson

    chain, _ = await household_clients(db, principal.household_id, include_paused=True)
    pauses = await paused(principal.household_id)
    sample = await db.scalar(select(Recommendation).where(
        Recommendation.household_id == principal.household_id, Recommendation.status == "open", Recommendation.scope == "holding")
        .order_by(Recommendation.actionable.desc(), Recommendation.priority.desc()).limit(1)) if review else None
    packet = ai_review.evidence_packet(sample) if sample else None

    async def one(c: Any) -> dict[str, Any]:
        ping = await test_client(c)
        out: dict[str, Any] = {"provider": c.provider, "model": c.model, "ping": ping,
                               "paused": bool(pauses.get(c.provider)), "paused_reason": (pauses.get(c.provider) or {}).get("reason")}
        if ping.get("ok") and packet is not None:
            import time as _t

            t0 = _t.perf_counter()
            v = await ai_review._one(c, packet)
            ok = v.stance != "error"
            out["review"] = {"ok": ok, "symbol": sample.symbol, "packet_chars": len(orjson.dumps(packet, default=str)),
                             "latency_ms": int((_t.perf_counter() - t0) * 1000),
                             "stance": v.stance if ok else None, "suggested_action": v.suggested_action if ok else None,
                             "plain": v.plain_verdict if ok else None,
                             "error": None if ok else (v.issues or [{}])[0].get("detail")}
            if ok and out["paused"]:
                await unpause(principal.household_id, c.provider)
                out["paused"] = False
        return out

    results = list(await asyncio.gather(*(one(c) for c in chain)))
    return {"results": results, "sample": sample.symbol if sample else None,
            "note": None if chain else "No AI provider configured — add one below or in .env."}


@router.delete("/ai-providers/{provider}", status_code=204, response_class=Response, response_model=None)
async def delete_provider(provider: str, db: DB, principal: Principal = require(P_AI_CONFIGURE)) -> None:
    p = await db.scalar(select(AIProvider).where(AIProvider.household_id == principal.household_id, AIProvider.provider == provider))
    if p:
        await db.delete(p)
        await db.commit()


# ---------------- Ask my portfolio (E25) ----------------
class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    profile_id: uuid.UUID | None = None


@router.post("/ask")
async def ask(body: AskIn, db: DB, principal: Principal = require(P_ADVISOR_READ)) -> dict[str, Any]:
    params = {"scope": "profile", "id": str(body.profile_id)} if body.profile_id else {"scope": "household"}
    h = await http.get(common.portfolio_url, "/api/v1/holdings", token=principal.token, params=params)
    recs = (await db.execute(select(Recommendation).where(Recommendation.household_id == principal.household_id, Recommendation.status == "open")
                             .order_by(Recommendation.priority.desc()).limit(25))).scalars().all()
    last = (await db.execute(select(AdvisorRun).where(AdvisorRun.household_id == principal.household_id, AdvisorRun.status == "done")
                             .order_by(AdvisorRun.started_at.desc()).limit(1))).scalar_one_or_none()
    context = {
        "summary": h.get("summary"), "profiles": h.get("profiles"),
        "holdings": [{k: r.get(k) for k in ("profile_name", "name", "symbol", "asset_type", "quantity", "avg_cost", "price", "market_value",
                                            "unrealised_pnl", "unrealised_pct", "xirr_pct", "day_change", "holding_days", "intent")} for r in h.get("holdings", [])][:80],
        "open_recommendations": [{"headline": r.headline, "action": r.action, "profile": (r.full_report.get("summary") or {}).get("profile"),
                                  "reasons": r.short_report.get("reasons"), "tax": r.short_report.get("tax_impact"), "confidence": r.confidence,
                                  "ai_consensus": r.ai_consensus} for r in recs],
        "market_regime": last.regime if last else None, "health": (last.stats or {}).get("health") if last else None,
    }
    return await narrate.ask(db, principal.household_id, body.question, context)
