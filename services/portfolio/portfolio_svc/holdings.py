"""Scope → holdings: load ledger rows, price them via market-svc, summarise."""
from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fm_common import http
from fm_common.config import settings
from fm_common.deps import Principal
from fm_common.logging import get_logger

from .ledger import Txn, build_positions, summarise, value_positions
from .models import HoldingPref, Portfolio, Profile, Transaction

log = get_logger(__name__)
NPS_NAV_MAX_AGE_DAYS = 45  # CRA statements are monthly-ish; older than this → suggest importing a new one
QUOTES_BUDGET_SECONDS = 6.0  # never let a slow price feed blank the dashboard; unpriced rows show at cost


def to_txn(t: Transaction) -> Txn:
    return Txn(str(t.id), str(t.instrument_id), t.symbol, t.asset_type, t.txn_type.value, t.trade_date,
               t.quantity, t.price, t.fees, t.amount, str(t.portfolio_id), date_estimated=is_estimated(t))


def is_estimated(t: Transaction) -> bool:
    """Rows written from a holdings statement (or a CAS opening balance) carry a guessed purchase date."""
    return (t.client_ref or "").startswith("hold:") or "estimated" in (t.notes or "").lower()


async def load_txns(db: AsyncSession, household_id: uuid.UUID, portfolio_ids: list[uuid.UUID]) -> list[Transaction]:
    if not portfolio_ids:
        return []
    stmt = select(Transaction).where(
        Transaction.household_id == household_id, Transaction.portfolio_id.in_(portfolio_ids), Transaction.deleted_at.is_(None)
    )
    return list((await db.execute(stmt)).scalars())


async def market_context(principal: Principal, instrument_ids: list[str], symbols: list[str], price_wait: float = 0) -> tuple[dict[str, Any], dict[str, Any]]:
    """Instrument metadata + live quotes, fetched in parallel. Either one failing or running slow
    degrades the result (names/prices missing → valued at cost) instead of failing holdings."""
    async def instruments_call() -> dict[str, Any]:
        if not instrument_ids:
            return {}
        rows = await http.post(settings.market_url, "/api/v1/market/instruments/batch", token=principal.token, json={"ids": instrument_ids})
        return {r["id"]: r for r in rows}

    async def quotes_call() -> dict[str, Any]:
        priced = [s for s in symbols if s]
        if not priced:
            return {}
        wait = min(max(price_wait, 4.0), 25.0)
        return await asyncio.wait_for(
            http.get(settings.market_url, "/api/v1/market/quotes", token=principal.token, params={"symbols": ",".join(priced), "wait": wait},
                     request_timeout=wait + 5),
            max(QUOTES_BUDGET_SECONDS, wait + 3),
        )

    inst_res, quote_res = await asyncio.gather(instruments_call(), quotes_call(), return_exceptions=True)
    if isinstance(inst_res, BaseException):
        log.warning("holdings.instruments_unavailable", error=str(inst_res) or type(inst_res).__name__)
        inst_res = {}
    if isinstance(quote_res, BaseException):
        log.warning("holdings.quotes_unavailable", error=str(quote_res) or type(quote_res).__name__)
        quote_res = {}
    return inst_res, quote_res


async def compute(
    db: AsyncSession, principal: Principal, portfolios: list[Portfolio], profiles: dict[uuid.UUID, Profile], price_wait: float = 0
) -> dict[str, Any]:
    today = date.today()
    txns = await load_txns(db, principal.household_id, [p.id for p in portfolios])
    ledger = [to_txn(t) for t in txns]
    accrual = {"epf", "vpf", "ppf", "fixed_deposit", "bond", "cash"}
    iids = sorted({t.instrument_id for t in ledger})
    syms = sorted({t.symbol for t in ledger if t.asset_type not in accrual})
    instruments, quotes = await market_context(principal, iids, syms, price_wait)

    pf_profile = {str(p.id): p.profile_id for p in portfolios}
    by_profile: dict[uuid.UUID, list[Txn]] = defaultdict(list)
    for t in ledger:
        by_profile[pf_profile[t.portfolio_id]].append(t)

    prefs = {
        (r.profile_id, str(r.instrument_id)): r
        for r in (await db.execute(select(HoldingPref).where(HoldingPref.profile_id.in_(list(by_profile)), HoldingPref.deleted_at.is_(None)))).scalars()
    } if by_profile else {}

    rows: list[dict[str, Any]] = []
    per_profile: list[dict[str, Any]] = []
    all_flows = []
    for profile_id, ptx in by_profile.items():
        positions = build_positions(ptx)
        prow = value_positions(positions, quotes, instruments, today)
        flows = [f for p in positions.values() for f in p.cashflows]
        all_flows.extend(flows)
        prof = profiles.get(profile_id)
        for r in prow:
            pref = prefs.get((profile_id, r["instrument_id"]))
            r["profile_id"] = str(profile_id)
            r["profile_name"] = prof.display_name if prof else None
            r["intent"] = pref.intent.value if pref and pref.intent else None
            r["intent_source"] = pref.intent_source if pref else None
            r["benchmark_symbol"] = instruments.get(r["instrument_id"], {}).get("benchmark_symbol")
            r["meta"] = instruments.get(r["instrument_id"], {}).get("meta") or {}
        rows.extend(prow)
        per_profile.append({
            "profile_id": str(profile_id), "name": prof.display_name if prof else None,
            "relationship": prof.relationship.value if prof else None, "color": prof.color if prof else None,
            **{k: v for k, v in summarise(prow, instruments, today, flows).items() if k not in ("by_sector",)},
        })
    summary = summarise(rows, instruments, today, all_flows)
    # Consolidated view: one row per instrument across profiles (lots per profile are kept in `rows`)
    unpriced = sum(1 for r in rows if not r["priced"] and r["quantity"] > 0)
    stale = sum(1 for r in rows if r.get("price_status") in ("stale", "statement") and r["quantity"] > 0)
    # NPS has no price feed: its NAV comes from the last imported statement. Only nag when that is old.
    nps_old = sorted({str(r.get("price_as_of") or "")[:10] for r in rows if r.get("price_status") == "nav_statement" and r["quantity"] > 0
                      and str(r.get("price_as_of") or "")[:10] < (today - timedelta(days=NPS_NAV_MAX_AGE_DAYS)).isoformat()})
    return {
        "as_of": today.isoformat(), "summary": summary, "profiles": per_profile, "unpriced_count": unpriced, "stale_count": stale,
        "nps_nav_as_of": nps_old[0] if nps_old else None,
        "holdings": sorted(rows, key=lambda r: -r["market_value"]),
    }
