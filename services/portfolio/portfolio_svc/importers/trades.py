"""Trade history from any broker — Zerodha, Upstox, ICICI Direct, Kotak, Paytm Money, SBI Securities, Groww,
Angel One, 5paisa, HDFC Securities, Motilal … — as CSV, XLSX or XLS.

Nothing is keyed on the broker: the table is found by its *columns* anywhere in any sheet (brokers put a
title, client details or a date range above it), using the aliases below. A layout it still can't read
falls back to "map the columns once" (``mapping.py``), which is then remembered for that layout.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from .base import ParsedTxn, dec, parse_date

# canonical field → header names seen in brokers' exports (compared after lower-casing, trimming and
# dropping bracketed units such as "(₹)" / "(Rs.)")
COLS: dict[str, tuple[str, ...]] = {
    "symbol": ("symbol", "tradingsymbol", "trading symbol", "stock symbol", "scrip", "scrip name", "scrip code", "script name", "script",
               "ticker", "stock", "stock name", "instrument", "instrument name", "security", "security name", "security description",
               "company", "company name", "contract description", "scheme", "scheme name", "scheme code", "amfi code", "name"),
    "isin": ("isin", "isin code", "isin no", "isin number"),
    "date": ("trade date", "trade_date", "date", "transaction date", "order date", "execution date", "settlement date", "txn date",
             "deal date", "order execution time", "execution date and time", "trade date time", "date & time"),
    "side": ("trade type", "trade_type", "buy/sell", "b/s", "buy / sell", "side", "action", "transaction type", "txn type", "type",
             "order type", "transaction", "bs", "buy sell", "b-s", "b / s"),
    "qty": ("quantity", "qty", "qty.", "traded quantity", "trade quantity", "units", "shares", "no of shares", "executed quantity",
            "filled quantity", "trade qty"),
    "price": ("price", "trade price", "rate", "traded price", "transaction price", "avg price", "average price", "execution price",
              "market rate", "gross rate", "net rate", "nav", "trade rate", "price per unit"),
    "amount": ("value", "amount", "trade value", "net amount", "total", "gross amount", "total amount", "consideration", "turnover",
               "net total", "traded value"),
    "fees": ("charges", "brokerage", "total charges", "fees", "total brokerage", "brokerage amount", "transaction charges"),
    "exchange": ("exchange", "exch", "exchange name", "market"),
    "trade_id": ("trade id", "trade_id", "trade no", "trade no.", "trade num", "trade number", "trade ref no", "order id", "order no",
                 "order number", "contract note no"),
    "segment": ("segment", "asset type", "asset_type", "instrument type", "product", "series"),
}
REQUIRED = ("date", "side", "qty")

SIDE = {"b": "buy", "buy": "buy", "bought": "buy", "purchase": "buy", "p": "buy", "buy (b)": "buy",
        "s": "sell", "sell": "sell", "sold": "sell", "sale": "sell", "redeem": "sell", "redemption": "sell", "sell (s)": "sell",
        "sip": "sip", "dividend": "dividend", "div": "dividend", "bonus": "bonus", "split": "split"}
DERIVATIVE = re.compile(r"\b(fut|futures|opt|options|ce|pe|fo|f&o|nfo|bfo|mcx|cds|currency|commodity)\b", re.I)


def norm(h: Any) -> str:
    s = re.sub(r"\((?:₹|rs\.?|inr|in ₹|in rs\.?)\)|₹", "", str(h or "").lower())
    return re.sub(r"\s+", " ", s).strip(" :.-*")


def header_map(row: list[Any], cols: dict[str, tuple[str, ...]] = COLS) -> dict[str, int]:
    """Which column is which field. Each column is used once; aliases are tried in priority order."""
    cells = [norm(c) for c in row]
    found: dict[str, int] = {}
    for key, names in cols.items():
        for n in names:
            idx = next((i for i, c in enumerate(cells) if c == n and i not in found.values()), None)
            if idx is not None:
                found[key] = idx
                break
    return found


def is_trade_header(h: dict[str, int]) -> bool:
    return all(k in h for k in REQUIRED) and ("symbol" in h or "isin" in h) and ("price" in h or "amount" in h)


def find_table(tables: list[tuple[str, list[list[Any]]]]) -> tuple[str, int, dict[str, int]] | None:
    for sheet, rows in tables:
        for i, r in enumerate(rows[:80]):
            h = header_map(r)
            if is_trade_header(h):
                return sheet, i, h
    return None


def _date(v: Any) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, int | float) and 20000 < v < 80000:  # an Excel serial date that wasn't formatted
        from datetime import timedelta

        return date(1899, 12, 30) + timedelta(days=int(v))
    return parse_date(str(v))


def _cell(row: list[Any], idx: int | None) -> Any:
    return row[idx] if idx is not None and idx < len(row) else None


TICKER = re.compile(r"^[A-Z0-9&][A-Z0-9&.\-]{0,24}$")


def name_columns(row: list[Any]) -> list[int]:
    """Every column that names the security (a file can have "Stock Symbol" *and* "Company Name")."""
    names = set(COLS["symbol"])
    return [i for i, c in enumerate(row) if norm(c) in names]


def pick_symbol(values: list[str]) -> tuple[str, str]:
    """(symbol, display name) from the name-like cells of a row: a ticker-looking value is the symbol, a value
    with spaces is the name."""
    vals = [v.strip() for v in values if v and v.strip()]
    ticker = next((v.upper() for v in vals if TICKER.match(v.upper()) and not v.isdigit()), None)
    code = next((v for v in vals if v.isdigit()), None)
    name = next((v for v in vals if " " in v), None) or (vals[0] if vals else "")
    return (ticker or code or name), name


def parse_tables(tables: list[tuple[str, list[list[Any]]]]) -> tuple[list[ParsedTxn], list[dict[str, Any]]]:
    hit = find_table(tables)
    if hit is None:
        return [], [{"row": 1, "error": "No trade table found (needs a date, buy/sell, quantity, a stock name or ISIN, and a price or value)."}]
    sheet, start, h = hit
    header = dict(tables)[sheet][start]
    ncols = name_columns(header) or ([h["symbol"]] if "symbol" in h else [])
    rows = dict(tables)[sheet][start + 1:]
    out: list[ParsedTxn] = []
    errors: list[dict[str, Any]] = []
    skipped_fno = 0
    for n, r in enumerate(rows, start=start + 2):
        sym_raw, name = pick_symbol([str(_cell(r, i) or "") for i in ncols])
        isin = str(_cell(r, h.get("isin")) or "").strip().upper() or None
        side_raw = str(_cell(r, h["side"]) or "").strip().lower()
        if not (name or isin) or not side_raw:
            continue  # blank / subtotal line
        if name.lower() in ("total", "grand total", "net total", "sub total"):
            continue
        try:
            seg = " ".join(str(_cell(r, h.get(k)) or "") for k in ("segment", "exchange")) + " " + name
            if DERIVATIVE.search(seg) and not isin:
                skipped_fno += 1
                continue
            side = SIDE.get(side_raw) or SIDE.get(side_raw.split()[0]) if side_raw else None
            if not side:
                raise ValueError(f"unknown buy/sell value {side_raw!r}")
            qty = abs(dec(_cell(r, h["qty"])))
            amount = abs(dec(_cell(r, h.get("amount"))))
            price = abs(dec(_cell(r, h.get("price")))) or (amount / qty if qty else dec(0))
            if not qty:
                continue
            seg_l = seg.lower()
            bse_code = sym_raw.isdigit() and len(sym_raw) == 6 and sym_raw.startswith("5")  # e.g. 500325 = Reliance on BSE
            is_mf = (((isin or "").startswith("INF") and not re.search(r"\b(etf|bees)\b", name, re.I))
                     or (sym_raw.isdigit() and not bse_code) or "mutual" in seg_l)
            asset_type = "mutual_fund" if is_mf else "etf" if "etf" in seg_l or re.search(r"bees\b|\betf\b", f"{name} {sym_raw}", re.I) else "stock"
            exch = str(_cell(r, h.get("exchange")) or "").upper()
            tid = str(_cell(r, h.get("trade_id")) or "").strip()
            d = _date(_cell(r, h["date"]))
            symbol = sym_raw if asset_type == "mutual_fund" else re.sub(r"-(EQ|BE|BZ|SM|ST)$", "", sym_raw.upper())
            out.append(ParsedTxn(
                symbol=symbol or isin or "", asset_type=asset_type, txn_type=side, trade_date=d, quantity=qty, price=price,
                amount=amount or qty * price, fees=abs(dec(_cell(r, h.get("fees")))),
                exchange="BSE" if "BSE" in exch or bse_code else "NSE", isin=isin, name=name or None,
                external_id=f"{tid}|{d}|{side}|{qty}" if tid else None, raw={"sheet": sheet, "row": n},
            ))
        except Exception as exc:
            errors.append({"row": n, "error": f"Could not read this row: {exc}"})
    if skipped_fno:
        errors.append({"row": "file", "level": "warning", "error": f"{skipped_fno} futures & options / commodity trade(s) skipped — only investments are tracked."})
    return out, errors


def parse(content: bytes) -> tuple[list[ParsedTxn], list[dict[str, Any]]]:
    from .holdings import _tables

    return parse_tables(_tables(content))
