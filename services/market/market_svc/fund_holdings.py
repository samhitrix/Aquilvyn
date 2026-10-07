"""MF look-through data (E16): which stocks are inside each fund the family holds.

* ``refresh`` — for every fund / ETF that anyone holds, download its fund house's latest monthly portfolio
  (``fund_sources.fetch_amc``) and store the matched schemes in ``market.fund_portfolios``. A fund house is
  only contacted when one of its schemes is missing or older than the last complete month, and at most once
  a day unless forced. Each fund house's outcome is recorded as a data source (Settings → Data sources).
  Schemes the fund house's file doesn't give us (JavaScript-only sites, old files) come from a public holdings
  API instead (``fund_api``). Liquid, debt, gold and silver funds are skipped: they hold no stocks.
* ``lookthrough`` — instrument ids → their stored holdings (what portfolio-svc's exposure engine reads).
"""
from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fm_common.logging import get_logger
from fm_common.redact import redact
from fm_common.redis import get_redis

from . import fund_api, fund_groww, health
from .fund_portfolio import match_score
from .fund_sources import Fetch, Render, fetch_amc
from .models import FundPortfolio, Instrument

log = get_logger(__name__)
ATTEMPT_KEY = "fm:funds:attempt:"
ATTEMPT_TTL = 20 * 3600
CODE_KEY = "fm:funds:code:"  # instrument id → AMFI code ("" = none), for ETFs / funds not keyed by their code
CODE_TTL = 7 * 86400
STALE_DAYS = 75  # a disclosure older than this (≈ two months) is shown as stale


NO_STOCKS_NAME = re.compile(r"(?i)\b(gold|silver|liquid|gilt|overnight|money\s*market|g[\s-]?sec|treasury|t[\s-]?bill|bonds?|sdl|"
                            r"floater|floating|credit\s*risk|duration|banking\s*(?:&|and)\s*psu|constant\s*maturity|"
                            r"target\s*maturity|1d\s*rate|crisil[\s-]*ibx|nifty\s*(?:aaa|psu|sdl|g[\s-]?sec))\b")
NO_STOCKS_CATEGORY = re.compile(r"(?i)debt\s+scheme|gold|silver|commodit")


def holds_no_stocks(name: str, category: str = "") -> bool:
    """Liquid / debt / gilt / gold / silver funds and ETFs: nothing to look through (not "missing data")."""
    if category and NO_STOCKS_CATEGORY.search(category) and "hybrid" not in category.lower():
        return True
    if "equity" in name.lower() or "hybrid" in category.lower():
        return False
    return bool(NO_STOCKS_NAME.search(name) or re.search(r"(?i)\bdebt\b", name))


_NOT_GROWTH = re.compile(r"(?i)\b(idcw|dividend|payout|reinvest|bonus|weekly|monthly|quarterly|daily|half[- ]?yearly|annual)\b")


def category_label(category: str) -> str:
    """'Open Ended Schemes(Equity Scheme - Multi Cap Fund)' → 'Multi Cap Fund'."""
    m = re.search(r"-\s*([^()]+?)\s*\)?\s*$", category or "")
    return (m.group(1) if m else category or "").strip()


def fund_peers(code: str, by_code: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The other funds in the same AMFI category, one per fund: Direct plan, Growth option (what you'd switch into).
    Other plans of the same fund are left out — moving from one plan of a lagging fund to another fixes nothing."""
    me = by_code.get(code)
    if not me or not me.get("category"):
        return {"category": None, "label": None, "peers": []}
    family = _family(me["name"])
    peers = []
    for c, m in by_code.items():
        n = m["name"]
        if c == code or m.get("category") != me["category"] or "direct" not in n.lower() or _NOT_GROWTH.search(n):
            continue
        if _family(n) == family:
            continue
        peers.append({"code": c, "name": n, "amc": m.get("amc")})
    return {"category": me["category"], "label": category_label(me["category"]), "self": {"code": code, "name": me["name"]}, "peers": peers}


def _family(name: str) -> str:
    """A fund's name without plan / option words: 'X Fund - Direct Plan - Growth' and 'X Fund - Regular - Growth' match."""
    n = re.sub(r"\(.*?\)", " ", name.lower())
    n = re.sub(r"\b(direct|regular|plan|growth|option|idcw|dividend|payout|reinvestment|bonus|fund|the)\b", " ", n)
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", n).split())


def source_key(amc: str) -> str:
    return f"fund_portfolio:{amc}"


def last_month_end(today: date) -> date:
    return today.replace(day=1) - timedelta(days=1)


def is_due(row: FundPortfolio | None, today: date) -> bool:
    """Fetch when we have nothing, or when last month's disclosure should exist (AMCs publish by ~the 10th)."""
    if row is None or row.as_of is None:
        return True
    target = last_month_end(today) if today.day >= 10 else last_month_end(last_month_end(today))
    return row.as_of < target.replace(day=1)


async def fund_codes(db: AsyncSession, instruments: list[Instrument]) -> dict[uuid.UUID, str]:
    """instrument id → AMFI scheme code. Mutual funds are keyed by their code; ETFs via ISIN, else by name."""
    from .providers import amfi

    out: dict[uuid.UUID, str] = {}
    need: list[Instrument] = []
    for i in instruments:
        if i.asset_type.value == "mutual_fund" and i.symbol.isdigit():
            out[i.id] = i.symbol
        elif i.asset_type.value in ("etf", "mutual_fund"):
            need.append(i)
    if need:
        r = get_redis()
        cached = await r.mget([CODE_KEY + str(i.id) for i in need])
        rest = []
        for i, c in zip(need, cached, strict=True):
            if c is not None:
                if c:
                    out[i.id] = c.decode() if isinstance(c, bytes) else c
            else:
                rest.append(i)
        master = await amfi.master() if rest else {}
        for i in rest:  # by ISIN, else the closest AMFI name (slow over ~15k schemes: remembered for a week)
            hit = master.get((i.isin or "").upper())
            code = hit["code"] if hit else ""
            if not code:
                best = max(((match_score(i.name or i.symbol, row["name"]), row["code"]) for row in master.values()), default=(0.0, ""))
                code = best[1] if best[0] >= 0.85 else ""
            if code:
                out[i.id] = code
            if master:  # a failed scheme-list download is not remembered as "no match"
                await r.set(CODE_KEY + str(i.id), code, ex=CODE_TTL)
    return out


async def held_targets(db: AsyncSession, codes: set[str] | None = None) -> dict[str, dict[str, Any]]:
    """{amfi_code: {name, amc}} for every fund / ETF instrument in use (optionally only ``codes``)."""
    from .providers import amfi

    insts = (await db.execute(select(Instrument).where(Instrument.asset_type.in_(["mutual_fund", "etf"]),
                                                        Instrument.is_active.is_(True)))).scalars().all()
    by_code = await amfi.by_code()
    out: dict[str, dict[str, Any]] = {}
    for code in set((await fund_codes(db, list(insts))).values()):
        if codes and code not in codes:
            continue
        meta = by_code.get(code)
        if meta and meta.get("amc") and not holds_no_stocks(meta["name"], meta.get("category", "")):
            out[code] = {"name": meta["name"], "amc": meta["amc"]}
    return out


async def _default_fetch(url: str) -> tuple[int, bytes, str, str]:
    from .providers import browser

    return await browser.get_bytes("fund_portfolio", url)


async def _default_render(url: str) -> tuple[int, str, str]:
    from .providers import headless

    return await headless.render(url)


def _renderer(fetch: Fetch | None) -> Render | None:
    """The headless browser when it's installed (Docker image) — never in tests that inject their own fetch."""
    from .providers import headless

    return _default_render if fetch is None and headless.available() else None


def _fresh(sc: dict[str, Any] | None, today: date) -> bool:
    return bool(sc and sc.get("as_of") and (today - sc["as_of"]).days <= STALE_DAYS)


def _newer(sc: dict[str, Any], have: dict[str, Any] | None) -> bool:
    return have is None or (sc.get("as_of") or date.min) > (have.get("as_of") or date.min)


async def _name_index() -> dict[str, str]:
    """company name → ISIN for NSE-listed shares (Groww lists holdings by name)."""
    from .providers import isin

    try:
        return await isin.name_index()
    except Exception as exc:  # noqa: BLE001 — without it holdings still line up by name
        log.warning("funds.name_index_failed", error=str(exc)[:160])
        return {}


async def refresh(db: AsyncSession, *, codes: set[str] | None = None, force: bool = False, fetch: Fetch | None = None,
                  today: date | None = None, render: Render | None = None) -> list[dict[str, Any]]:
    """Fetch what's due, store it, and report per fund house: [{amc, ok, as_of, matched, unmatched, error, tried, skipped}]."""
    today = today or date.today()
    targets = await held_targets(db, codes)
    if not targets:
        from .providers import amfi

        if not await amfi.master():
            return [{"amc": "AMFI scheme list", "ok": False, "matched": {}, "unmatched": [], "tried": [],
                     "error": "AMFI's scheme list (portal.amfiindia.com) could not be downloaded, so your funds can't be matched to "
                              "their fund houses — check internet access; see Settings → Data sources → AMFI scheme list"}]
        return []
    rows = {r.amfi_code: r for r in (await db.execute(select(FundPortfolio).where(FundPortfolio.amfi_code.in_(list(targets))))).scalars()}
    by_amc: dict[str, dict[str, dict[str, Any]]] = {}
    for code, t in targets.items():
        by_amc.setdefault(t["amc"], {})[code] = t
    r = get_redis()
    report = []
    render = render or _renderer(fetch)
    api_down: str | None = None  # a source that answered 5xx / not at all isn't asked again in this run
    groww_down: str | None = None
    names = await _name_index() if fetch is None else {}
    for amc, tg in sorted(by_amc.items()):
        due = {c: t for c, t in tg.items() if force or is_due(rows.get(c), today)}
        if not due:
            report.append({"amc": amc, "ok": True, "skipped": "up to date", "as_of": _iso(max((rows[c].as_of for c in tg if rows[c].as_of), default=None))})
            continue
        if not force and await r.get(ATTEMPT_KEY + amc):
            report.append({"amc": amc, "ok": None, "skipped": "tried in the last 20 h — will retry later"})
            continue
        await r.set(ATTEMPT_KEY + amc, "1", ex=ATTEMPT_TTL)
        f = fetch or _default_fetch
        matched: dict[str, dict[str, Any]] = {}
        why: dict[str, list[str]] = {c: [] for c in due}
        tried: list[dict[str, Any]] = []
        # 1. Groww's fund pages: every scheme in one format
        for code, t in due.items():
            if groww_down:
                why[code].append(groww_down)
                continue
            got = await fund_groww.fetch_scheme(code, t["name"], amc, f, names)
            tried += got["tried"]
            if got["ok"]:
                matched[code] = {**got["scheme"], "score": 1.0, "source": "Groww", "url": got["url"]}
            else:
                why[code].append(f"Groww: {got['error']}")
                if got["unreachable"]:
                    groww_down = f"Groww: {got['error']} — not asked again this run"
        # 2. the fund house's own monthly file, for what Groww didn't give (or only an old month of)
        need = {c: t for c, t in due.items() if not _fresh(matched.get(c), today)}
        res: dict[str, Any] = {}
        if need:
            try:
                res = await fetch_amc(amc, need, f, today=today, render=render)
            except Exception as exc:  # noqa: BLE001 — one fund house never stops the others
                res = {"amc": amc, "ok": False, "error": f"{type(exc).__name__}: {exc}", "matched": {}, "tried": []}
            tried += res.get("tried", [])
            for code, sc in res.get("matched", {}).items():
                if _newer(sc, matched.get(code)):
                    matched[code] = {**sc, "source": "fund house file", "url": sc.get("url") or res.get("url")}
            for code in need:
                if code not in res.get("matched", {}) and res.get("error"):
                    why[code].append(f"fund house file: {res['error']}")
        # 3. the public holdings API, last
        for code, t in due.items():
            if code in matched:
                continue
            if api_down:
                why[code].append(api_down)
                continue
            got = await fund_api.fetch_scheme(code, t["name"], f)
            tried += got["tried"]
            if got["ok"]:
                matched[code] = {**got["scheme"], "score": 1.0, "source": "holdings API", "url": got["url"]}
            else:
                why[code].append(f"holdings API: {got['error']}")
                if got.get("unreachable"):
                    api_down = f"holdings API down ({got['error'].rsplit('(', 1)[-1].rstrip(')')}) — not asked again this run"
        for code, sc in matched.items():
            await _store(db, code, amc, sc, sc.get("url"))
        await db.commit()
        unmatched = [c for c in due if c not in matched]
        dates = [sc["as_of"] for sc in matched.values() if sc.get("as_of")]
        as_of = max(dates) if dates else None
        sources = sorted({sc["source"] for sc in matched.values()})
        error = None if matched else " · ".join(dict.fromkeys(w for c in due for w in why[c])) or "failed"
        if matched:
            missing = f"; not found: {', '.join(due[c]['name'] for c in unmatched)}" if unmatched else ""
            await health.record(source_key(amc), ok=True, count=len(matched), note=f"portfolio as of {_iso(as_of)} via {' + '.join(sources)}{missing}")
        else:
            await health.record(source_key(amc), ok=False, error=redact(error))
        report.append({"amc": amc, "ok": bool(matched), "as_of": _iso(as_of), "url": res.get("url"), "sources": sources,
                       "warning": res.get("warning") if "fund house file" in sources else None,
                       "matched": {c: {"scheme": sc["scheme"], "score": sc["score"], "holdings": len(sc["holdings"]), "equity_pct": sc["equity_pct"],
                                       "as_of": _iso(sc.get("as_of")), "source": sc["source"]} for c, sc in matched.items()},
                       "unmatched": [{"code": c, "name": due[c]["name"], "why": redact(" · ".join(why[c])) or None,
                                      "closest": (res.get("closest") or {}).get(c)} for c in unmatched],
                       "error": redact(error) if error else None, "tried": tried})
    return report


async def _store(db: AsyncSession, code: str, amc: str, s: dict[str, Any], url: str | None) -> None:
    row = await db.get(FundPortfolio, code) or FundPortfolio(amfi_code=code)
    row.amc, row.scheme, row.as_of, row.source_url, row.sheet = amc, s["scheme"][:255], s.get("as_of"), (url or "")[:1000], s.get("sheet", "")[:255]
    row.match_score = Decimal(str(s.get("score", 0)))
    row.equity_pct, row.total_pct = Decimal(str(s["equity_pct"])), Decimal(str(s["total_pct"]))
    row.holdings = s["holdings"]
    row.fetched_at = datetime.now(UTC)
    db.add(row)


async def lookthrough(db: AsyncSession, instrument_ids: list[uuid.UUID], today: date | None = None) -> dict[str, Any]:
    """{instrument_id: {code, scheme, amc, as_of, age_days, stale, equity_pct, holdings}} for those that have data."""
    today = today or date.today()
    insts = (await db.execute(select(Instrument).where(Instrument.id.in_(instrument_ids)))).scalars().all()
    from .providers import amfi

    codes = await fund_codes(db, list(insts))
    by_code = await amfi.by_code() if codes else {}
    rows = {r.amfi_code: r for r in (await db.execute(select(FundPortfolio).where(FundPortfolio.amfi_code.in_(list(set(codes.values())))))).scalars()}
    names = {i.id: i.name or i.symbol for i in insts}
    out: dict[str, Any] = {}
    for iid, code in codes.items():
        row = rows.get(code)
        meta = by_code.get(code) or {}
        if row is None and holds_no_stocks(meta.get("name") or names.get(iid, ""), meta.get("category", "")):
            out[str(iid)] = {"code": code, "available": False, "no_stocks": True}
            continue
        if row is None:
            out[str(iid)] = {"code": code, "available": False}
            continue
        age = (today - row.as_of).days if row.as_of else None
        out[str(iid)] = {"code": code, "available": True, "scheme": row.scheme, "amc": row.amc, "as_of": _iso(row.as_of), "age_days": age,
                         "stale": age is None or age > STALE_DAYS, "equity_pct": float(row.equity_pct or 0), "holdings": row.holdings}
    return out


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None
