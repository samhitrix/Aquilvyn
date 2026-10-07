"""Capital-gains / Tax P&L statements from any broker (Zerodha "Tax P&L", Groww / Upstox / ICICI
"Capital gains" reports, CSV or Excel) → realised-gain lots and dividend / interest income.

Nothing here is broker-specific: every sheet (or the CSV) is scanned for tables whose *columns*
look like sales (symbol/ISIN + sell value or price + buy value / price / profit) or income
(symbol + amount + ex-date / per-share). The title row above a table ("Equity - Long Term",
"Debt ETF", "Mutual Funds", "Dividends") gives its category; when there is no title the holding
period decides. Each row is then checked:

* profit ≈ sell − buy (flag otherwise)            * a zero buy value (cost basis missing)
* sell date not before buy date                    * the stated term matches the holding period
* lot-level totals reconcile with the statement's own summary figures (when it has them)

Lot-level tables (with dates) are preferred; per-symbol summary tables that repeat the same sales
are used only to cross-check, so nothing is counted twice.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .base import dec, parse_date

TOL = Decimal("1")


@dataclass
class TaxRow:
    record: str                    # "gain" (a sale lot) | "income" (dividend / interest)
    symbol: str
    isin: str | None = None
    asset: str = "equity"          # equity | equity_mf | debt | debt_mf | other | fno
    term: str = "short"            # intraday | short | long | business   (gains)  ·  dividend | interest (income)
    buy_date: date | None = None
    sell_date: date | None = None  # income: ex-date / record date
    quantity: Decimal = Decimal(0)
    buy_value: Decimal = Decimal(0)
    sell_value: Decimal = Decimal(0)
    profit: Decimal = Decimal(0)   # income: the amount
    taxable_profit: Decimal = Decimal(0)
    days_held: int | None = None
    per_unit: Decimal | None = None
    section: str = ""
    flags: list[str] = field(default_factory=list)
    source_row: str = ""


COLS: dict[str, tuple[str, ...]] = {
    "symbol": ("symbol", "scrip", "scrip name", "scrip code", "script name", "script", "stock symbol", "stock", "stock name", "security", "security name", "instrument",
               "trading symbol", "tradingsymbol", "scheme", "scheme name", "fund", "fund name", "name", "company", "company name", "particulars"),
    "isin": ("isin", "isin code", "isin no"),
    "buy_date": ("entry date", "buy date", "purchase date", "date of purchase", "acquisition date", "buy trade date", "date of acquisition", "open date"),
    "sell_date": ("exit date", "sell date", "sale date", "date of sale", "redemption date", "sell trade date", "date of transfer", "transfer date", "close date"),
    "qty": ("quantity", "qty", "units", "sell quantity", "sell qty", "no of shares", "shares", "redeemed units"),
    "buy_value": ("buy value", "purchase value", "cost of acquisition", "cost", "buy amount", "total buy value", "purchase amount", "acquisition cost",
                  "buy value (with charges)", "invested value"),
    "sell_value": ("sell value", "sale value", "sell amount", "sale consideration", "full value of consideration", "redemption value",
                   "redemption amount", "total sell value", "sale amount", "sell value (net of charges)"),
    "buy_price": ("buy price", "purchase price", "buy avg", "avg buy price", "buy rate", "average buy price", "purchase nav", "buy nav"),
    "sell_price": ("sell price", "sale price", "sell avg", "avg sell price", "sell rate", "average sell price", "redemption nav", "sell nav"),
    "profit": ("profit", "realized p&l", "realised p&l", "realized pnl", "realised pnl", "p&l", "pnl", "gain", "gain/loss", "capital gain",
               "realised gain", "realized gain", "profit/loss", "net p&l", "realised profit", "realized profit", "gain / loss"),
    "taxable": ("taxable profit", "taxable gain", "taxable capital gain", "taxable p&l"),
    "days": ("period of holding", "holding period", "days held", "holding days", "holding period (days)", "no of days held"),
    "term": ("term", "gain type", "capital gain type", "st/lt", "type of gain"),
    "ex_date": ("ex-date", "ex date", "record date", "payment date", "dividend date", "date"),
    "per_unit": ("dividend per share", "interest per quantity", "dividend per unit", "dps", "rate per share", "interest per unit"),
    "amount": ("net dividend amount", "net interest amount", "dividend amount", "interest amount", "net amount", "amount", "gross amount"),
}
PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
DEBT_FUND = re.compile(r"\b(debt|liquid|gilt|bond|money market|overnight|corporate|credit risk|banking and psu|banking & psu|duration|floater|treasury|fixed maturity)\b", re.I)
BROKERS = ("zerodha", "groww", "upstox", "icici", "hdfc securities", "kotak", "angel", "5paisa", "dhan", "motilal", "sharekhan", "paytm money",
           "sbi securities", "sbicap", "axis direct", "iifl", "edelweiss", "nuvama", "fyers", "geojit")


def _norm(v: Any) -> str:
    s = re.sub(r"\(.*?\)|₹|rs\.?|inr", "", str(v or "").lower())
    return re.sub(r"\s+", " ", s).strip(" :.-")


def _header(row: list[Any]) -> dict[str, int]:
    cells = [_norm(c) for c in row]
    raw = [re.sub(r"\s+", " ", str(c or "").lower()).strip() for c in row]
    found: dict[str, int] = {}
    for key, names in COLS.items():
        for n in names:
            for i, (c, r) in enumerate(zip(cells, raw, strict=False)):
                if i in found.values():
                    continue
                if c == n or r == n:
                    found[key] = i
                    break
            if key in found:
                break
    return found


def _is_gain_table(h: dict[str, int]) -> bool:
    return ("symbol" in h or "isin" in h) and ("sell_value" in h or "sell_price" in h) and bool({"buy_value", "buy_price", "profit"} & h.keys())


def _is_income_table(h: dict[str, int], row: list[Any], title: str) -> bool:
    if not ("symbol" in h or "isin" in h) or "amount" not in h or "sell_value" in h:
        return False
    text = " ".join(str(c or "").lower() for c in row) + " " + title.lower()
    return "dividend" in text or "interest" in text


def _cells(row: list[Any]) -> list[Any]:
    return [c for c in row if c not in (None, "") and str(c).strip() != ""]


def _title(row: list[Any]) -> str | None:
    c = _cells(row)
    if len(c) == 1 and isinstance(c[0], str) and not re.fullmatch(r"[\d.,\-\s₹]+", c[0]):
        return c[0].strip()
    return None


def _date(v: Any) -> date | None:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return parse_date(str(v))
    except ValueError:
        return None


def _num(v: Any) -> Decimal:
    try:
        return dec(v)
    except ValueError:
        return Decimal(0)


def classify(title: str) -> tuple[str | None, str | None]:
    """Section / sheet title → (asset, term); None where the title doesn't say."""
    t = title.lower()
    asset: str | None = None
    if any(k in t for k in ("f&o", "futures", "options", "currency", "commodity", "derivative")):
        asset = "fno"
    elif "debt etf" in t or re.search(r"\bdebt\b", t):
        asset = "debt"
    elif "non equity" in t or "non-equity" in t or "gold" in t:
        asset = "other"
    elif "mutual fund" in t or re.search(r"\bmf\b", t):
        asset = "equity_mf"
    elif "equity" in t or "stock" in t or "share" in t or "buyback" in t:
        asset = "equity"
    term = ("intraday" if "intraday" in t or "speculat" in t else "short" if "short" in t
            else "long" if "long" in t else None)
    return asset, term


def _term_for(asset: str, days: int | None, same_day: bool) -> str | None:
    if asset == "fno":
        return "business"
    if asset in ("debt", "debt_mf"):
        return "short"  # debt funds / debt ETFs bought after Apr 2023: taxed at slab whatever the period
    if days is None:
        return None
    if same_day and asset == "equity":
        return "intraday"
    return "long" if days > 365 else "short"


def _meta(tables: list[tuple[str, list[list[Any]]]]) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    for sheet, rows in tables:
        for r in rows[:40]:
            c = _cells(r)
            if len(c) < 1:
                continue
            label = _norm(c[0])
            val = str(c[1]).strip() if len(c) > 1 else ""
            if label in ("pan", "pan no", "pan number") and PAN_RE.search(val.upper()):
                meta.setdefault("pan", PAN_RE.search(val.upper()).group(0))  # type: ignore[union-attr]
            elif label in ("client id", "client code", "ucc", "trading account", "account id", "demat account") and val:
                meta.setdefault("client_id", val)
            elif label in ("client name", "name", "investor name", "account holder") and val:
                meta.setdefault("name", val)
            text = " ".join(str(x) for x in c)
            m = re.search(r"from\s+(\S+)\s+to\s+(\S+)", text, re.I)
            if m and "period" not in meta:
                d1, d2 = _date(m.group(1)), _date(m.group(2))
                if d1 and d2:
                    meta["period"] = (d1, d2)
            if "pan" not in meta:
                p = PAN_RE.search(text.upper())
                if p and label.startswith("pan"):
                    meta["pan"] = p.group(0)
        blob = (sheet + " " + " ".join(str(x) for r in rows[:8] for x in _cells(r))).lower()
        for b in BROKERS:
            if b in blob:
                meta.setdefault("broker", b.title() if b != "icici" else "ICICI Direct")
    return meta


def looks_like_tax_pnl(content: bytes) -> bool:
    from .holdings import _tables

    try:
        tables = _tables(content)
    except Exception:
        return False
    for _, rows in tables:
        for r in rows[:400]:
            h = _header(r)
            if _is_gain_table(h) and ("sell_date" in h or "profit" in h):
                return True
    return False


def parse(content: bytes) -> tuple[list[TaxRow], list[dict[str, Any]], dict[str, Any]]:
    """→ (rows, issues, meta). meta: pan, client_id, name, broker, period, reported totals, charges, checks, notes."""
    from .holdings import _tables

    tables = _tables(content)
    meta = _meta(tables)
    lots: list[TaxRow] = []
    aggregated: list[TaxRow] = []
    income: list[TaxRow] = []
    reported: dict[tuple[str, str], Decimal] = {}
    reported_income: dict[str, Decimal] = {}
    charges = Decimal(0)
    notes: list[str] = []

    for sheet, rows in tables:
        title = ""
        h: dict[str, int] | None = None
        mode = ""  # "gain" | "income" | "kv" | "ledger"
        sheet_asset, _ = classify(sheet)
        for n, row in enumerate(rows):
            c = _cells(row)
            if not c:
                h, mode = None, ""
                continue
            t = _title(row)
            if t is not None:
                title, h, mode = t, None, ""
                continue
            hh = _header(row)
            if _is_gain_table(hh):
                h, mode = hh, "gain"
                continue
            if _is_income_table(hh, row, title):
                amount_head = str(row[hh["amount"]] or "").lower()
                h, mode = {**hh, "_interest": int("interest" in amount_head and "dividend" not in amount_head)}, "income"
                continue
            low = [str(x).strip().lower() for x in c]
            if low[:2] in (["account head", "amount"],) or (len(low) >= 3 and low[0] == "particulars" and "debit" in low):
                h, mode = {"a": 0}, ("kv" if low[0] == "account head" else "ledger")
                continue
            if mode == "gain" and h is not None:
                row_ = _gain_row(sheet, title, sheet_asset, h, row, n)
                if row_ is not None:
                    (lots if row_.sell_date else aggregated).append(row_)
                continue
            if mode == "income" and h is not None:
                inc = _income_row(sheet, title, h, row, n)
                if inc is not None:
                    income.append(inc)
                continue
            if mode == "kv" and len(c) >= 2 and isinstance(c[-1], int | float):
                charges += Decimal(str(c[-1]))
                continue
            if mode == "ledger" and len(c) >= 2:
                text = str(c[0])
                if "gift" in text.lower():
                    notes.append(text.strip())
                continue
            if len(c) == 2 and isinstance(c[0], str) and isinstance(c[1], int | float) and re.search(r"total (dividend|interest)", c[0], re.I):
                reported_income[_norm(c[0])] = Decimal(str(c[1]))
                continue
            # "Equity Long Term profit | 227947.06" style summary lines
            if len(c) == 2 and isinstance(c[0], str) and isinstance(c[1], int | float) and re.search(r"profit|p&l|gain", c[0], re.I):
                reported[(sheet_asset or "equity", _norm(c[0]))] = Decimal(str(c[1]))

    issues: list[dict[str, Any]] = []
    gains = lots
    if not lots and aggregated:
        gains = aggregated
        for r in gains:
            r.flags.append("no_dates")
        issues.append({"row": "statement", "level": "warning",
                       "error": "Only per-symbol totals found (no buy/sell dates) — gains are imported without lot dates."})
    if meta.get("period"):
        p0, p1 = meta["period"]
        for r in gains:
            if r.sell_date and not (p0 <= r.sell_date <= p1):
                r.flags.append("outside_period")
    rows_out = gains + income
    if not rows_out:
        return [], [{"row": "file", "error": "No sales (capital gains) or dividend tables found. Expected columns like Symbol, "
                                              "Buy/Sell date, Quantity, Buy value, Sell value, Profit."}], meta

    checks = _reconcile(gains, aggregated if lots else [], reported)
    for label, value in reported_income.items():
        kind = "interest" if "interest" in label else "dividend"
        got = sum((r.profit for r in income if r.term == kind), Decimal(0))
        checks.append({"label": label.capitalize(), "reported": float(value), "parsed": float(got), "ok": abs(got - value) <= TOL})
    for ck in checks:
        if not ck["ok"]:
            issues.append({"row": "check", "level": "warning",
                           "error": f"{ck['label']}: statement says ₹{ck['reported']:,.2f}, rows add up to ₹{ck['parsed']:,.2f}."})
    flagged = defaultdict(list)
    for r in gains:
        for f in r.flags:
            flagged[f].append(r)
    for f, rs in flagged.items():
        if f in FLAG_TEXT:
            issues.append({"row": "check", "level": "warning", "error": FLAG_TEXT[f].format(n=len(rs), syms=", ".join(sorted({r.symbol for r in rs})[:6]))})
    for note in notes[:5]:
        issues.append({"row": "note", "level": "warning", "error": f"Off-market transfer on the statement: {note}"})
    meta.update({"charges": float(charges), "checks": checks, "notes": notes,
                 "period": [d.isoformat() for d in meta["period"]] if meta.get("period") else None})
    return rows_out, issues, meta


FLAG_TEXT = {
    "zero_cost": "{n} sale(s) show a buy value of 0 ({syms}) — either bonus shares (₹0 is correct) or shares transferred in with the cost missing. The Tax page checks each one automatically.",
    "profit_mismatch": "{n} row(s) where profit ≠ sell − buy ({syms}) — charges or grandfathering; the statement's figure is used.",
    "term_mismatch": "{n} row(s) whose short/long-term label doesn't match the holding period ({syms}) — the statement's label is used; please check.",
    "date_order": "{n} row(s) sold before they were bought ({syms}) — please check.",
    "outside_period": "{n} sale(s) dated outside the statement period ({syms}).",
    "buyback": "{n} buyback sale(s) ({syms}) — since Oct 2024 buyback proceeds are taxed as dividend; review with your CA.",
}


def _get(row: list[Any], h: dict[str, int], k: str) -> Any:
    i = h.get(k)
    return row[i] if i is not None and i < len(row) else None


def _gain_row(sheet: str, title: str, sheet_asset: str | None, h: dict[str, int], row: list[Any], n: int) -> TaxRow | None:
    symbol = str(_get(row, h, "symbol") or "").strip()
    isin = str(_get(row, h, "isin") or "").strip().upper() or None
    if not symbol and not isin:
        return None
    if symbol.lower() in ("total", "grand total", "net total", "sub total", "subtotal"):
        return None
    qty = _num(_get(row, h, "qty"))
    buy = _num(_get(row, h, "buy_value")) if "buy_value" in h else _num(_get(row, h, "buy_price")) * qty
    sell = _num(_get(row, h, "sell_value")) if "sell_value" in h else _num(_get(row, h, "sell_price")) * qty
    profit = _num(_get(row, h, "profit")) if "profit" in h else sell - buy
    taxable = _num(_get(row, h, "taxable")) if "taxable" in h else profit
    bdate, sdate = _date(_get(row, h, "buy_date")), _date(_get(row, h, "sell_date"))
    days: int | None = None
    if "days" in h and str(_get(row, h, "days") or "").strip() != "":
        try:
            days = int(_num(_get(row, h, "days")))
        except (ValueError, ArithmeticError):
            days = None
    if bdate and sdate:
        real = (sdate - bdate).days
        flags_days = days is not None and abs(days - real) > 2
        days = real
    else:
        flags_days = False
    asset_t, term_t = classify(title)
    term_col = str(_get(row, h, "term") or "").lower()
    asset = asset_t or sheet_asset or "equity"
    if asset == "equity_mf" and DEBT_FUND.search(symbol):
        asset = "debt_mf"  # taxed like debt (slab); kept apart so it still reconciles with the MF totals
    if term_col:
        term_t = term_t or ("long" if "long" in term_col or term_col.startswith("lt") else "short" if "short" in term_col or term_col.startswith("st") else None)
    computed = _term_for(asset, days, bool(bdate and sdate and bdate == sdate))
    term = term_t or computed or "short"
    if asset == "fno":
        term = "business"
    r = TaxRow("gain", symbol or isin or "", isin, asset, term, bdate, sdate, qty, buy, sell, profit, taxable, days,
               section=f"{sheet} › {title}".strip(" ›"), source_row=f"{sheet}!{n + 1}")
    if buy == 0 and sell > 0:
        r.flags.append("zero_cost")
    elif "profit" in h and abs((sell - buy) - profit) > max(TOL, sell * Decimal("0.005")):
        r.flags.append("profit_mismatch")
    if bdate and sdate and sdate < bdate:
        r.flags.append("date_order")
    if term_t and computed and term_t != computed and asset in ("equity", "equity_mf") and {term_t, computed} <= {"short", "long"}:
        r.flags.append("term_mismatch")
    if flags_days:
        r.flags.append("days_mismatch")
    if "buyback" in title.lower():
        r.flags.append("buyback")
    if not term_t and not computed:
        r.flags.append("term_unknown")
    return r


def _income_row(sheet: str, title: str, h: dict[str, int], row: list[Any], n: int) -> TaxRow | None:
    symbol = re.sub(r"[#*]+$", "", str(_get(row, h, "symbol") or "").strip())
    isin = str(_get(row, h, "isin") or "").strip().upper() or None
    amount = _num(_get(row, h, "amount"))
    if (not symbol and not isin) or symbol.lower() in ("total", "grand total") or amount == 0:
        return None
    kind = "interest" if h.get("_interest") else "dividend"
    per = _num(_get(row, h, "per_unit")) if "per_unit" in h else None
    return TaxRow("income", symbol or isin or "", isin, "equity", kind, None, _date(_get(row, h, "ex_date")), _num(_get(row, h, "qty")),
                  profit=amount, taxable_profit=amount, per_unit=per, section=f"{sheet} › {title}".strip(" ›"), source_row=f"{sheet}!{n + 1}")


def bucket_of(r: TaxRow) -> str:
    return f"{r.asset}:{r.term}"


def _reconcile(gains: list[TaxRow], aggregated: list[TaxRow], reported: dict[tuple[str, str], Decimal]) -> list[dict[str, Any]]:
    """Compare what the rows add up to with the statement's own totals."""
    sums: dict[str, tuple[Decimal, Decimal]] = defaultdict(lambda: (Decimal(0), Decimal(0)))
    for r in gains:
        p, t = sums[bucket_of(r)]
        sums[bucket_of(r)] = (p + r.profit, t + r.taxable_profit)
    out: list[dict[str, Any]] = []
    for (sheet_asset, label), value in reported.items():
        asset, term = classify(label)
        asset = "equity_mf" if sheet_asset == "equity_mf" and asset in (None, "equity") else asset or sheet_asset
        if asset == "fno":
            keys = [k for k in sums if k.startswith("fno:")]
            if ("option" in label) != ("future" in label):
                continue  # options vs futures split isn't visible per row; the F&O total is checked via the others
        else:
            keys = [f"{asset}:{term}"] if term else [k for k in sums if k.startswith(f"{asset}:")]
        if asset == "equity_mf":  # MF rows classified debt by fund name; the statement lumps them in with the rest
            keys += [k.replace("equity_mf", "debt_mf") for k in keys]
        p = sum((sums[k][0] for k in keys if k in sums), Decimal(0))
        t = sum((sums[k][1] for k in keys if k in sums), Decimal(0))
        ok = min(abs(p - value), abs(t - value)) <= max(TOL, abs(value) * Decimal("0.005"))
        out.append({"label": ("Mutual funds · " if sheet_asset == "equity_mf" else "") + label.capitalize(), "reported": float(value), "parsed": float(t if abs(t - value) < abs(p - value) else p), "ok": ok})
    if aggregated:  # per-symbol summary tables vs the lot-level rows (brokers often lump MFs in with "equity" there)
        def eq(k: str) -> str:
            return k.replace("equity_mf:", "equity:").replace("debt_mf:", "equity:")
        agg: dict[str, Decimal] = defaultdict(Decimal)
        for r in aggregated:
            agg[eq(bucket_of(r))] += r.profit
        lot: dict[str, Decimal] = defaultdict(Decimal)
        for k, (pp, _t) in sums.items():
            lot[eq(k)] += pp
        for k, v in agg.items():
            p = lot.get(k, Decimal(0))
            ok = abs(p - v) <= max(TOL, abs(v) * Decimal("0.005"))
            out.append({"label": f"Per-symbol summary {k.replace(':', ' ')}", "reported": float(v), "parsed": float(p), "ok": ok})
    return out
