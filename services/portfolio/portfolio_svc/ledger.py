"""Holdings & Valuation Engine (E06) — pure functions over the ledger.

* FIFO lots (Indian tax law for securities is FIFO) → open lots, realised P&L per sale.
* Splits/bonuses restate open lots so cost basis and holding period stay correct.
* Accrual instruments (EPF/VPF/PPF/FD/bond/cash) are rupee-denominated (1 unit = ₹1) and grow
  at ``interest_rate`` % p.a. from the later of each flow or the last recorded interest credit.
* XIRR over signed cash flows (Newton–Raphson with bisection fallback).
"""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

D0 = Decimal(0)
ACCRUAL_TYPES = {"epf", "vpf", "ppf", "fixed_deposit", "bond", "cash"}


@dataclass(slots=True)
class Txn:
    id: str
    instrument_id: str
    symbol: str
    asset_type: str
    txn_type: str
    trade_date: date
    quantity: Decimal
    price: Decimal
    fees: Decimal = D0
    amount: Decimal = D0
    portfolio_id: str = ""
    date_estimated: bool = False  # from a holdings statement / opening balance: the real purchase date is unknown


@dataclass(slots=True)
class Lot:
    buy_date: date
    qty: Decimal
    cost_per_unit: Decimal  # incl. allocated fees
    txn_id: str
    date_estimated: bool = False


@dataclass(slots=True)
class RealisedSale:
    sell_date: date
    buy_date: date
    qty: Decimal
    buy_cost: Decimal
    proceeds: Decimal
    txn_id: str

    @property
    def gain(self) -> Decimal:
        return self.proceeds - self.buy_cost

    @property
    def holding_days(self) -> int:
        return (self.sell_date - self.buy_date).days


@dataclass(slots=True)
class Position:
    instrument_id: str
    symbol: str
    asset_type: str
    lots: list[Lot] = field(default_factory=list)
    realised: list[RealisedSale] = field(default_factory=list)
    dividends: Decimal = D0
    interest_credited: Decimal = D0
    cashflows: list[tuple[date, float]] = field(default_factory=list)  # investor's view: out < 0
    first_date: date | None = None
    last_interest_date: date | None = None

    @property
    def qty(self) -> Decimal:
        return sum((lot.qty for lot in self.lots), D0)

    @property
    def invested(self) -> Decimal:
        return sum((lot.qty * lot.cost_per_unit for lot in self.lots), D0)

    @property
    def avg_cost(self) -> Decimal:
        q = self.qty
        return self.invested / q if q else D0

    @property
    def realised_pnl(self) -> Decimal:
        return sum((r.gain for r in self.realised), D0)


class LedgerError(ValueError):
    pass


def build_positions(txns: Iterable[Txn], strict: bool = False) -> dict[str, Position]:
    positions: dict[str, Position] = {}
    order = {"split": 0, "bonus": 1}  # same-day corporate actions apply before trades
    for t in sorted(txns, key=lambda t: (t.trade_date, order.get(t.txn_type, 2), t.id)):
        p = positions.get(t.instrument_id)
        if p is None:
            p = positions[t.instrument_id] = Position(t.instrument_id, t.symbol, t.asset_type)
        p.first_date = p.first_date or t.trade_date
        accrual = t.asset_type in ACCRUAL_TYPES
        qty = t.amount if accrual and t.txn_type != "split" else t.quantity
        gross = t.amount if t.amount else t.quantity * t.price

        if t.txn_type in ("buy", "sip", "contribution", "switch_in"):
            if qty <= 0:
                continue
            cost = (gross + t.fees) / qty
            p.lots.append(Lot(t.trade_date, qty, cost, t.id, t.date_estimated))
            p.cashflows.append((t.trade_date, -float(gross + t.fees)))
        elif t.txn_type == "interest":
            p.lots.append(Lot(t.trade_date, qty, D0, t.id))  # interest is gain, not invested capital
            p.interest_credited += qty
            p.last_interest_date = t.trade_date
            if not accrual:  # interest paid out on a bond
                p.cashflows.append((t.trade_date, float(gross)))
        elif t.txn_type == "bonus":
            p.lots.append(Lot(t.trade_date, qty, D0, t.id))  # zero-cost units, holding period from allotment
        elif t.txn_type == "split":
            ratio = t.quantity
            if ratio <= 0:
                raise LedgerError(f"Invalid split ratio {ratio}")
            for lot in p.lots:  # holding period continues from original purchase
                lot.qty *= ratio
                lot.cost_per_unit /= ratio
        elif t.txn_type in ("sell", "withdrawal", "switch_out"):
            remaining = qty
            proceeds_per_unit = (gross - t.fees) / qty if qty else D0
            if remaining > p.qty + Decimal("0.0001"):
                if strict:
                    raise LedgerError(f"Selling {remaining} {t.symbol} but only {p.qty} held on {t.trade_date}")
                remaining = p.qty
            while remaining > 0 and p.lots:
                lot = p.lots[0]
                take = min(lot.qty, remaining)
                p.realised.append(RealisedSale(t.trade_date, lot.buy_date, take, take * lot.cost_per_unit, take * proceeds_per_unit, t.id))
                lot.qty -= take
                remaining -= take
                if lot.qty <= Decimal("0.00000001"):
                    p.lots.pop(0)
            p.cashflows.append((t.trade_date, float(gross - t.fees)))
        elif t.txn_type == "dividend":
            p.dividends += gross
            p.cashflows.append((t.trade_date, float(gross)))
        elif t.txn_type == "fee":
            p.cashflows.append((t.trade_date, -float(gross or t.fees)))
    return positions


def accrued_value(p: Position, annual_rate_pct: float, as_of: date) -> Decimal:
    """Balance + interest accrued (compound annually, pro-rata) since the later of each lot's
    date and the last recorded interest credit (so passbook entries are never double-counted)."""
    r = annual_rate_pct / 100
    total = 0.0
    for lot in p.lots:
        start = max(lot.buy_date, p.last_interest_date or lot.buy_date)
        years = max(0.0, (as_of - start).days / 365.0)
        total += float(lot.qty) * (1 + r) ** years
    return Decimal(str(round(total, 2)))


def xnpv(rate: float, flows: list[tuple[date, float]]) -> float:
    t0 = flows[0][0]
    try:
        return sum(cf / (1 + rate) ** ((d - t0).days / 365.0) for d, cf in flows)
    except (OverflowError, ZeroDivisionError):
        return math.inf if rate < 0 else 0.0


MIN_XIRR_DAYS = 365  # AMFI/SEBI convention: under a year, report absolute return, not annualised


def xirr(flows: list[tuple[date, float]], min_days: int = 0) -> float | None:
    """Annualised internal rate of return. Never raises: returns None when it can't be computed
    (no sign change, span shorter than ``min_days``, or no root in −99.99%…+10,000%)."""
    flows = sorted((d, cf) for d, cf in flows if cf and math.isfinite(cf))
    if len(flows) < 2 or all(cf >= 0 for _, cf in flows) or all(cf <= 0 for _, cf in flows):
        return None
    span = (flows[-1][0] - flows[0][0]).days
    if span < max(1, min_days):
        return None
    t0 = flows[0][0]
    rate = 0.1
    try:
        for _ in range(100):  # Newton
            f = df = 0.0
            for d, cf in flows:
                t = (d - t0).days / 365.0
                f += cf / (1 + rate) ** t
                df -= t * cf / (1 + rate) ** (t + 1)
            if df == 0:
                break
            new = rate - f / df
            if not math.isfinite(new) or new <= -0.9999 or new > 100:
                break
            if abs(new - rate) < 1e-9:
                return new
            rate = new
    except (OverflowError, ZeroDivisionError):
        pass
    lo, hi = -0.9999, 100.0  # bisection fallback
    flo, fhi = xnpv(lo, flows), xnpv(hi, flows)
    if (flo > 0) == (fhi > 0):
        return None  # no root in range → not meaningful
    for _ in range(300):
        mid = (lo + hi) / 2
        fm = xnpv(mid, flows)
        if abs(fm) < 1e-7:
            return mid
        if (fm > 0) == (flo > 0):
            lo, flo = mid, fm
        else:
            hi = mid
    return (lo + hi) / 2


def value_positions(
    positions: dict[str, Position],
    prices: dict[str, dict[str, Any]],
    instruments: dict[str, dict[str, Any]],
    as_of: date,
) -> list[dict[str, Any]]:
    """Attach live prices / accruals → rows ready for dashboards and the advisor."""
    rows: list[dict[str, Any]] = []
    for iid, p in positions.items():
        qty = p.qty
        inst = instruments.get(iid, {})
        if qty <= Decimal("0.00000001") and not p.realised and not p.dividends:
            continue
        q: dict[str, Any] = {}
        if p.asset_type in ACCRUAL_TYPES:
            rate = float((inst.get("meta") or {}).get("interest_rate") or 0)
            value = accrued_value(p, rate, as_of)
            price = prev = None
            day_change = Decimal(0)
        else:
            q = prices.get(p.symbol) or {}
            price = q.get("price")
            prev = q.get("prev_close")
            value = qty * Decimal(str(price)) if price is not None else p.invested
            day_change = qty * Decimal(str(price - prev)) if price is not None and prev is not None else Decimal(0)
        invested = p.invested
        unrealised = value - invested
        flows = [*p.cashflows, (as_of, float(value))] if qty > 0 else list(p.cashflows)
        estimated = bool(p.lots) and any(lot.date_estimated for lot in p.lots)
        # an annualised return over made-up purchase dates is meaningless (it showed "XIRR 54% p.a.")
        x = None if estimated else xirr(flows, MIN_XIRR_DAYS)
        holding_days = (as_of - p.lots[0].buy_date).days if p.lots else None
        rows.append({
            "instrument_id": iid, "symbol": p.symbol, "asset_type": p.asset_type, "name": inst.get("name", p.symbol),
            "sector": inst.get("sector"), "quantity": float(qty), "avg_cost": round(float(p.avg_cost), 4),
            "invested": round(float(invested), 2), "price": price, "prev_close": prev, "market_value": round(float(value), 2),
            "day_change": round(float(day_change), 2), "unrealised_pnl": round(float(unrealised), 2),
            "unrealised_pct": round(float(unrealised / invested * 100), 2) if invested else None,
            "realised_pnl": round(float(p.realised_pnl), 2), "dividends": round(float(p.dividends), 2),
            "xirr_pct": round(x * 100, 2) if x is not None else None,
            "first_buy_date": p.first_date.isoformat() if p.first_date else None,
            "oldest_open_lot_date": p.lots[0].buy_date.isoformat() if p.lots else None,
            "holding_days": holding_days, "dates_estimated": estimated, "priced": price is not None or p.asset_type in ACCRUAL_TYPES,
            # "live" | "stale" (last good quote, source down) | "statement" (price printed on an imported statement)
            # | "nav_statement" (NPS: the statement NAV is the only source — expected, not a data problem)
            "price_status": None if p.asset_type in ACCRUAL_TYPES or price is None else
                            ("nav_statement" if p.asset_type == "nps" and q.get("source") == "statement" else
                             "statement" if q.get("source") == "statement" else "stale" if q.get("stale") else "live"),
            "price_as_of": q.get("ts") if p.asset_type not in ACCRUAL_TYPES else None,
            "lots": [{"buy_date": lot.buy_date.isoformat(), "qty": float(lot.qty), "cost": round(float(lot.cost_per_unit), 4)} for lot in p.lots],
        })
    return rows


ASSET_CLASS = {
    "stock": "equity", "etf": "equity", "reit": "real_estate", "mutual_fund": "equity", "nps": "equity",
    "epf": "debt", "vpf": "debt", "ppf": "debt", "fixed_deposit": "debt", "bond": "debt", "gold": "gold",
    "crypto": "alternative", "cash": "cash",
}


def asset_class(row: dict[str, Any], inst: dict[str, Any] | None = None) -> str:
    meta = (inst or {}).get("meta") or {}
    if row["asset_type"] == "mutual_fund":
        cat = str(meta.get("mf_category") or "equity")
        return "debt" if cat.startswith("debt") else "hybrid" if cat.startswith("hybrid") else "equity"
    if row["asset_type"] == "nps":
        return {"E": "equity", "C": "debt", "G": "debt", "A": "alternative"}.get(meta.get("scheme", "E"), "equity")
    if row["asset_type"] == "etf":
        label = f"{row.get('name') or ''} {row.get('symbol') or ''}".lower()
        if "gold" in label or "silver" in label:
            return "gold"
        if any(k in label for k in ("liquid", "gilt", "bond", "g-sec", "gsec", "money market", "sdl", "bharat bond")):
            return "debt"
    return ASSET_CLASS.get(row["asset_type"], "other")


def summarise(rows: list[dict[str, Any]], instruments: dict[str, dict[str, Any]], as_of: date, all_flows: list[tuple[date, float]]) -> dict[str, Any]:
    invested = sum(r["invested"] for r in rows)
    value = sum(r["market_value"] for r in rows)
    day = sum(r["day_change"] for r in rows)
    prev_value = value - day
    alloc: dict[str, float] = defaultdict(float)
    by_type: dict[str, float] = defaultdict(float)
    by_sector: dict[str, float] = defaultdict(float)
    for r in rows:
        alloc[asset_class(r, instruments.get(r["instrument_id"]))] += r["market_value"]
        by_type[r["asset_type"]] += r["market_value"]
        if r.get("sector"):
            by_sector[r["sector"]] += r["market_value"]
    guessed = sum(r["market_value"] for r in rows if r.get("dates_estimated"))
    if value and guessed / value > 0.2:
        x = None  # most purchase dates are guesses (holdings statements): show the absolute return instead
    else:
        x = xirr([*all_flows, (as_of, value)], MIN_XIRR_DAYS) if value else xirr(all_flows, MIN_XIRR_DAYS)
    pct = lambda d: {k: {"value": round(v, 2), "pct": round(v / value * 100, 2) if value else 0} for k, v in sorted(d.items(), key=lambda kv: -kv[1])}  # noqa: E731
    return {
        "invested": round(invested, 2), "market_value": round(value, 2), "unrealised_pnl": round(value - invested, 2),
        "unrealised_pct": round((value - invested) / invested * 100, 2) if invested else None,
        "realised_pnl": round(sum(r["realised_pnl"] for r in rows), 2), "dividends": round(sum(r["dividends"] for r in rows), 2),
        "day_change": round(day, 2), "day_change_pct": round(day / prev_value * 100, 2) if prev_value else None,
        "xirr_pct": round(x * 100, 2) if x is not None else None,
        "allocation": pct(alloc), "by_asset_type": pct(by_type), "by_sector": pct(by_sector),
        "holdings_count": sum(1 for r in rows if r["quantity"] > 0), "unpriced": [r["symbol"] for r in rows if not r["priced"]],
    }
