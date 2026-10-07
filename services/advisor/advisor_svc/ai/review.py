"""Multi-AI Review orchestration (E24): send each recommendation's evidence packet to every
enabled provider in parallel, store verdicts, compute the consensus badge.

  all agree                         -> verified
  no disagree, ≥1 caution           -> caution
  any disagree                      -> needs_review   (both sides shown in the report)
  no provider configured / all fail -> rules_only     (always explicit, never silent)
Reviews are cached by the recommendation's input hash, so unchanged calls aren't re-reviewed."""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fm_common.logging import get_logger

from ..config import settings
from ..models import AIReview, Recommendation
from .base import AIProviderClient, Verdict
from .registry import describe_error, error_kind, household_clients, pause, pause_seconds

log = get_logger(__name__)


PACKET_MAX_CHARS = 6000  # ≈ 1.5–2 k tokens: fits free tiers (e.g. Groq's 8 k tokens/minute) with room for the answer
_SHORT_KEYS = ("action", "rule_action", "verdict", "confidence", "rules_confidence", "horizon", "intent", "asset_type", "price",
               "headline", "do", "reasons", "tax_impact", "what_would_change", "suggestion", "rupee_impact", "urgent")
_TECH_KEYS = ("price", "rsi14", "sma50", "sma200", "pct_from_52w_high", "pct_from_52w_low", "return_1m_pct", "return_3m_pct",
              "return_1y_pct", "max_drawdown_1y_pct", "relative_strength_3m_pct", "atr_pct", "macd_hist", "volume_ratio_20d")


def _pick(d: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: d[k] for k in keys if isinstance(d, dict) and d.get(k) is not None}


def _clip(v: Any, n: int) -> Any:
    return v if not isinstance(v, str) or len(v) <= n else v[: n - 1] + "…"


def evidence_packet(rec: Recommendation, slim: bool = False) -> dict[str, Any]:
    """What the reviewer needs — the call, the numbers behind it, the rule that fired — and nothing else
    (no chart series, no list of every rule that didn't match). ``slim`` drops to the bare essentials when a
    provider says the request is too large."""
    full = rec.full_report or {}
    ev = full.get("evidence") or {}
    sr = rec.short_report or {}
    trace = full.get("decision_trace") or {}
    dq = full.get("data_quality") or {}
    short = _pick(sr, _SHORT_KEYS)
    short["reasons"] = [_clip(r, 160) for r in (sr.get("reasons") or [])[: 3 if slim else 5]]
    tech = ev.get("technical") or {}
    fund = ev.get("fundamental") or {}
    evidence: dict[str, Any] = {
        "position": _pick(ev.get("position"), ("quantity", "avg_cost", "price", "invested", "market_value", "unrealised_pct",
                                               "xirr_pct", "holding_days", "dates_estimated")),
        "technical": {"values": _pick(tech.get("values"), _TECH_KEYS if not slim else _TECH_KEYS[:8]),
                      "signals": [_clip(x.get("label"), 70) for x in (tech.get("signals") or [])[: 3 if slim else 6] if isinstance(x, dict)]}
        if tech else None,
        "fundamental": {"available": fund.get("available"), "score": fund.get("score"), "verdict": fund.get("verdict"),
                        "pillars": fund.get("pillars"), "red_flags": (fund.get("red_flags") or [])[:4],
                        "metrics": {m.get("key"): m.get("value") for m in (fund.get("metrics") or [])[: 8 if slim else 14] if isinstance(m, dict)}}
        if fund else None,
        "fund": {k: v for k, v in (ev.get("fund") or {}).items() if k not in ("benchmark_risk", "rolling")} or None,
        "drawdown": _pick(ev.get("drawdown"), ("from_cost_pct", "from_high_pct", "threshold_pct", "triggered")),
        "sizing": _pick(ev.get("sizing"), ("weight_pct", "cap_pct", "over_cap", "stop_price")),
        "tax": _pick(ev.get("tax"), ("days_to_ltcg", "short_term_gain", "long_term_gain", "est_tax_if_sold_now", "saving_if_wait")),
        "risk": None if slim else _pick(ev.get("risk"), ("volatility_pct", "max_drawdown_pct", "sharpe", "cagr_pct")),
        "market": {"regime": (ev.get("regime") or {}).get("regime")},
    }
    evidence = {k: v for k, v in evidence.items() if v}
    matched = trace.get("matched_rule")
    opinion = sr.get("rule_action") == "REVIEW"
    packet: dict[str, Any] = {
        # data missing → the rules made no call: ask the AI for its own analyst opinion instead of a verification
        **({"mode": "opinion", "missing": [_clip(i.get("message"), 140) for i in (dq.get("issues") or [])][:5],
            "holding": {"name": rec.name, "symbol": rec.symbol, "asset_type": rec.asset_type}} if opinion else {}),
        "recommendation": short,
        "evidence": evidence,
        "data_quality": {"grade": dq.get("grade"), "score": dq.get("score"),
                         "issues": [_clip(i.get("message"), 140) for i in (dq.get("issues") or [])][:5]},
        "decision_trace": {"matched_rule": matched, "rulebook_version": trace.get("rulebook_version"),
                           "composite": (trace.get("composite") or {}).get("score") if isinstance(trace.get("composite"), dict) else trace.get("composite")},
    }
    if not slim:
        packet["risks_and_counter_arguments"] = [_clip(r, 200) for r in (full.get("risks_and_counter_arguments") or [])[:3]]
        packet["history"] = [{k: h.get(k) for k in ("at", "action", "confidence") if k in h} for h in (full.get("history") or [])[:3] if isinstance(h, dict)]
    if not slim and len(_json(packet)) > PACKET_MAX_CHARS:
        return evidence_packet(rec, slim=True)
    return packet


def _json(o: Any) -> str:
    import orjson

    return orjson.dumps(o, default=str).decode()


def consensus(verdicts: list[Verdict]) -> str:
    ok = [v for v in verdicts if v.stance != "error"]
    if not ok:
        return "ai_failed" if verdicts else "rules_only"  # an AI was asked but every call failed — not the same as "none configured"
    if any(v.stance == "disagree" for v in ok):
        return "needs_review"
    if any(v.stance == "caution" for v in ok):
        return "caution"
    return "verified"


AI_DELTA = {"verified": 0.10, "caution": -0.10, "needs_review": -0.25}  # an independent check agreeing / doubting moves the score visibly


# how cautious an action is: the AI may move a call towards HOLD, never away from it
CAUTION_STEP = {"EXIT": "TRIM", "TRIM": "HOLD", "ADD": "HOLD", "ACCUMULATE": "HOLD", "SWITCH": "HOLD"}
AI_OVERRULE_MIN_CONFIDENCE = 0.6


def _towards_caution(rule_action: str, ai_action: str | None) -> bool:
    if not ai_action or ai_action == rule_action:
        return False
    if ai_action == "HOLD":
        return rule_action in CAUTION_STEP
    return CAUTION_STEP.get(rule_action) == ai_action  # e.g. sell all → sell part


def apply_ai_to_verdict(rec: Recommendation, verdicts: list[Verdict]) -> None:
    """Fold the AI review into the call people see:
    * confidence moves (agree +10 pts, caution −10, disagree −25) and the verdict follows it;
    * a confident disagreement (≥ 60 %) whose own suggestion is MORE cautious (sell → hold, sell all → sell part,
      buy → hold) replaces the action — the AI can stop a trade, never start one;
    * a suggestion for a different or bigger trade is shown, not applied ("Groq suggests …");
    * a data-gate hold ('data missing') isn't "verified": the AI can only agree to wait."""
    from ..report import verdict_for

    sr = dict(rec.short_report or {})
    if rec.scope != "holding" or "rule_action" not in sr:
        return
    ok = [v for v in verdicts if v.stance != "error"]
    lead = ok[0] if ok else None
    sr["ai_view"] = ({"provider": lead.provider, "model": lead.model, "stance": lead.stance, "suggested_action": lead.suggested_action,
                      "plain": lead.plain_verdict, "confidence": lead.confidence} if lead else None)
    if sr.get("decided_by") == "fund_plan":
        # a portfolio-level decision (one fund per role, what to hold / sell over time / where to switch): the AI judged
        # this fund on its own, so its view is shown as a comment — it never rewrites or overrules the plan
        if lead and lead.suggested_action and lead.suggested_action != rec.action:
            sr["ai_suggests"] = {"provider": lead.provider, "action": lead.suggested_action, "plain": lead.plain_verdict, "applied": False,
                                 "why_not": "this is part of your fund plan (decided across all your funds); the AI looked at this fund alone"}
        else:
            sr.pop("ai_suggests", None)
        rec.short_report = sr
        return
    if sr["rule_action"] == "REVIEW":
        # the data to judge is missing, so the rules made no call — show the AI's own analyst opinion, clearly
        # labelled (general knowledge, may be outdated; advisory only). It never becomes an automatic action.
        if lead and lead.suggested_action and lead.suggested_action != "REVIEW":
            label = {"EXIT": "Sell", "TRIM": "Reduce", "HOLD": "Hold", "ADD": "Add", "ACCUMULATE": "Add gradually", "SWITCH": "Switch"}.get(
                lead.suggested_action, lead.suggested_action.title())
            term = {"short_term": "short-term", "long_term": "long-term"}.get(lead.horizon or "", "")
            sr["ai_opinion"] = {"provider": lead.provider, "model": lead.model, "action": lead.suggested_action, "label": label,
                                "horizon": lead.horizon, "confidence": lead.confidence, "plain": lead.plain_verdict,
                                "why": lead.rationale, "risks": lead.counter_case}
            name = rec.name or rec.symbol or "this holding"
            sr["verdict"] = f"AI opinion: {label}"
            sr["headline"] = f"{name} — AI opinion: {label}{f' ({term})' if term else ''}"
            sr["do"] = (f"{lead.plain_verdict or label} — {lead.provider.title()}'s opinion from general knowledge (live fundamentals "
                        "unavailable, may be outdated). Advisory only: check with your CA / adviser before trading.")
            rec.headline = sr["headline"]
        rec.short_report = sr
        return
    base = float(sr.get("rules_confidence", rec.confidence))
    delta = AI_DELTA.get(rec.ai_consensus, 0.0)
    conf = round(max(0.05, min(0.95, base + delta)), 2)
    rule_action = sr["rule_action"]
    rs = sr.get("rule_suggestion") or {}
    name = rec.name or rec.symbol or "this holding"
    who = lead.provider.title() if lead else "the AI"
    overrule = (lead is not None and lead.stance == "disagree" and (lead.confidence or 0) >= AI_OVERRULE_MIN_CONFIDENCE
                and _towards_caution(rule_action, lead.suggested_action))
    if overrule and lead and lead.suggested_action == "TRIM":
        q = rs.get("quantity")
        half = {**rs, "quantity": (max(1, int(q // 2)) if isinstance(q, int | float) and q >= 2 else q),
                "value": round(float(rs.get("value") or 0) / 2, 2)}
        v = verdict_for("TRIM", max(conf, 0.75), name, None, sugg=half, asset_type=sr.get("asset_type") or getattr(rec, "asset_type", None) or "stock",
                        price=sr.get("price"))
        v["do"] = f"{v['do']} ({who} disagreed with selling everything: {lead.plain_verdict or 'see the AI review'})"
    elif overrule and lead:
        v = {"action": "HOLD", "label": "Hold", "conviction": "medium", "leaning": rule_action, "headline": f"Hold {name}",
             "do": f"Do nothing — keep holding. The rules said {rule_action.lower()}, but {who} disagreed: {lead.plain_verdict or 'see the AI review'}"}
    else:
        why = None
        if rec.ai_consensus == "needs_review":
            why = f"the rules said {rule_action.lower()}, but {who} disagreed — see the AI review"
        elif rec.ai_consensus == "caution":
            why = f"the rules lean {rule_action.lower()}, but {who} urged caution"
        v = verdict_for(rule_action, conf, name, rs.get("note"), why, sugg=rs,
                        asset_type=sr.get("asset_type") or getattr(rec, "asset_type", None) or "stock", price=sr.get("price"))
        if v["action"] == rule_action and sr.get("do_prefix"):  # e.g. the funds to switch into — kept through the review
            v["do"] = f"{sr['do_prefix']} {v['do']}"
    if lead and lead.suggested_action and lead.suggested_action not in (rule_action, v["action"]) and not overrule:
        sr["ai_suggests"] = {"provider": lead.provider, "action": lead.suggested_action, "plain": lead.plain_verdict,
                             "applied": False, "why_not": "the AI can make a call more cautious, but not start a trade the rules don't support"}
    else:
        sr.pop("ai_suggests", None)
    sr.update({"action": v["action"], "verdict": v["label"], "conviction": v["conviction"], "leaning": v["leaning"], "do": v["do"],
               "headline": v["headline"], "confidence": conf, "ai_adjusted": delta != 0.0 or overrule, "rules_confidence": base,
               "ai_overruled": bool(overrule),
               "suggestion": rs if v["action"] == rule_action else ({"type": "sell", **half} if overrule and v["action"] == "TRIM" else {"type": "none", "note": None}),
               "urgent": bool(sr.get("urgent")) and v["conviction"] == "high" and not overrule})
    if v["action"] != rule_action and v["action"] == "HOLD":
        sr["rupee_impact"] = 0
    rec.short_report = sr
    rec.action, rec.headline, rec.confidence = v["action"], v["headline"], conf
    rec.actionable = v["action"] not in ("HOLD", "REVIEW")
    if v["conviction"] != "high" and rec.bucket == "urgent":
        rec.bucket = "focus" if v["action"] != "HOLD" else "fyi"


def verdicts_of(rows: list[AIReview]) -> list[Verdict]:
    """The latest successful review per provider (rows newest first) as verdicts."""
    latest: dict[str, Verdict] = {}
    for r in rows:
        if r.stance != "error":
            latest.setdefault(r.provider, Verdict(r.provider, r.model, r.stance, r.confidence, rationale=r.rationale or "",
                                                  counter_case=r.counter_case or "", suggested_action=r.suggested_action,
                                                  plain_verdict=r.plain_verdict or "", horizon=r.horizon))
    return list(latest.values())


async def carry_review(db: AsyncSession, rec: Recommendation) -> str:
    """Give a (re)built call the AI's view it already has, so "reviewed by AI" never silently disappears:
    * a review on exactly these inputs → the call is verified as before;
    * only a review of earlier inputs → that review stays valid and shown ("reviewed 29 Sep · re-check queued")
      until the new one arrives — never "pending" in the meantime;
    * no review ever → pending. Returns "current" | "earlier" | "none"."""
    rows = list((await db.execute(select(AIReview).where(AIReview.recommendation_id == rec.id)
                                  .order_by(AIReview.created_at.desc()))).scalars())
    ok = [r for r in rows if r.stance != "error"]
    if not ok:
        rec.ai_consensus = "pending" if rec.ai_consensus in ("verified", "caution", "needs_review") else rec.ai_consensus
        return "none"
    same = [r for r in ok if r.input_hash == rec.input_hash]
    use = same or [r for r in ok if r.input_hash == ok[0].input_hash]  # the most recent review round
    verdicts = verdicts_of(use)
    rec.ai_consensus = consensus(verdicts)
    apply_ai_to_verdict(rec, verdicts)
    sr = dict(rec.short_report or {})
    if same:
        sr.pop("ai_stale", None)
    else:
        sr["ai_stale"] = {"reviewed_at": use[0].created_at.isoformat(), "provider": use[0].provider}
    rec.short_report = sr
    return "current" if same else "earlier"


def rotate(chain: list[AIProviderClient], avoid: set[str]) -> list[AIProviderClient]:
    """Ask a different AI than last time (or than the one that wrote the summary): those go to the back."""
    return [c for c in chain if c.provider not in avoid] + [c for c in chain if c.provider in avoid]


async def _spent_this_month(db: AsyncSession, household_id: uuid.UUID) -> float:
    start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return float(await db.scalar(select(func.coalesce(func.sum(AIReview.cost_usd), 0)).where(AIReview.household_id == household_id, AIReview.created_at >= start)) or 0)


async def _one(client: AIProviderClient, packet: dict[str, Any]) -> Verdict:
    try:
        return await asyncio.wait_for(client.review(packet), timeout=180)
    except Exception as exc:
        err = describe_error(exc)
        log.warning("ai.review_failed", provider=client.provider, status=err["status"], error=err["error"][:200])
        detail = (f"HTTP {err['status']}: " if err["status"] else "") + err["error"] + (f" — {err['hint']}" if err["hint"] else "")
        kind = error_kind(err)
        return Verdict(client.provider, client.model, "error",
                       issues=[{"type": "error", "detail": detail, "kind": kind, "quota": kind in ("rate_minute", "quota")}])


def _retry_after(detail: str) -> float:
    """'Please try again in 7.5s' / '1m2s' / '450ms' → seconds to wait (capped; default 20 s)."""
    import re

    m = re.search(r"try again in\s+(?:(\d+)m)?\s*(\d+(?:\.\d+)?)(ms|s)?", detail or "", re.I)
    if not m:
        return 20.0
    secs = int(m.group(1) or 0) * 60 + float(m.group(2)) / (1000 if m.group(3) == "ms" else 1)
    return max(1.0, min(secs + 1, 65.0))


async def review_with(client: AIProviderClient, rec: Recommendation, packet: dict[str, Any]) -> list[Verdict]:
    """One provider's review of one call, recovering from what a retry can fix:
    too large → again with the slim packet; per-minute rate limit → wait what the provider asks, once."""
    out = [await _one(client, packet)]
    kind = (out[-1].issues or [{}])[0].get("kind") if out[-1].stance == "error" else None
    if kind == "too_large":
        out.append(await _one(client, evidence_packet(rec, slim=True)))
    elif kind == "rate_minute":
        await asyncio.sleep(_retry_after(out[-1].issues[0]["detail"]))
        out.append(await _one(client, packet))
    return out


async def review_recommendations(db: AsyncSession, household_id: uuid.UUID, rec_ids: list[uuid.UUID], force: bool = False) -> dict[str, Any]:
    reviewers, _ = await household_clients(db, household_id)
    recs = list((await db.execute(select(Recommendation).where(Recommendation.id.in_(rec_ids)))).scalars())
    if not reviewers:
        if (await household_clients(db, household_id, include_paused=True))[0]:
            return {"reviewed": 0, "mode": "all_paused"}  # connected but resting after rate limits: retried on the next run
        for r in recs:
            r.ai_consensus = "rules_only"
        await db.commit()
        return {"reviewed": 0, "mode": "rules_only"}
    if await _spent_this_month(db, household_id) >= settings.ai_review_monthly_budget_usd:
        log.warning("ai.budget_exhausted", household_id=str(household_id))
        return {"reviewed": 0, "mode": "budget_exhausted"}
    reviewed = 0
    results: list[dict[str, Any]] = []
    chain = list(reviewers)  # priority order: the first that answers reviews it; the next ones only on failure
    for rec in recs[: settings.ai_review_max_per_run]:
        existing = list((await db.execute(select(AIReview).where(AIReview.recommendation_id == rec.id, AIReview.input_hash == rec.input_hash))).scalars())
        if not force and any(e.stance != "error" for e in existing):
            await carry_review(db, rec)  # already reviewed on these inputs: make sure the call shows it
            continue
        packet = evidence_packet(rec)
        verdicts: list[Verdict] = []
        last = {e.provider for e in existing if e.stance != "error"} | set((await db.execute(select(AIReview.provider).where(
            AIReview.recommendation_id == rec.id, AIReview.stance != "error").order_by(AIReview.created_at.desc()).limit(1))).scalars())
        for c in rotate(list(chain), last):  # a different AI than the one that reviewed this call last time
            tries = await review_with(c, rec, packet)
            verdicts.extend(tries)
            v = tries[-1]
            if v.stance != "error":
                break
            if v.issues and v.issues[0].get("quota"):  # still rate-limited / out of quota: skip it (10 min or a day)
                await pause(household_id, c.provider, v.issues[0]["detail"], pause_seconds(v.issues[0].get("kind")))
                chain.remove(c)
        for v in verdicts:
            db.add(AIReview(
                id=uuid.uuid4(), recommendation_id=rec.id, household_id=household_id, provider=v.provider, model=v.model,
                stance=v.stance, confidence=v.confidence, issues=v.issues, rationale=v.rationale, counter_case=v.counter_case,
                suggested_action=v.suggested_action, plain_verdict=(v.plain_verdict or None), horizon=v.horizon,
                input_hash=rec.input_hash, latency_ms=v.latency_ms, cost_usd=v.cost_usd,
            ))
        ok = [v for v in verdicts if v.stance != "error"]
        if ok:
            rec.ai_consensus = consensus(ok)
            apply_ai_to_verdict(rec, ok)
            rec.short_report = {k: v for k, v in (rec.short_report or {}).items() if k != "ai_stale"}
        elif (await carry_review(db, rec)) == "none":  # this attempt failed, but an earlier review still stands
            rec.ai_consensus = "ai_failed" if verdicts else "rules_only"
        reviewed += 1
        results = [{"provider": v.provider, "model": v.model, "stance": v.stance, "latency_ms": v.latency_ms,
                    "error": v.issues[0]["detail"] if v.stance == "error" and v.issues else None} for v in verdicts]
        if not chain:
            break  # every provider failed / is paused — stop rather than hammer them
    await db.commit()
    return {"reviewed": reviewed, "providers": [c.provider for c in reviewers], "results": results,
            "consensus": recs[0].ai_consensus if len(recs) == 1 else None}
