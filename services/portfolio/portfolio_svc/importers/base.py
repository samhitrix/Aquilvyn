from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(slots=True)
class ParsedTxn:
    symbol: str                 # exchange symbol (RELIANCE) or AMFI code or ISIN
    asset_type: str             # stock | etf | mutual_fund
    txn_type: str               # buy | sell | sip | dividend | …
    trade_date: date
    quantity: Decimal
    price: Decimal
    amount: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    exchange: str | None = "NSE"
    isin: str | None = None
    name: str | None = None
    external_id: str | None = None   # trade id / folio+date — used for idempotency
    raw: dict[str, Any] = field(default_factory=dict)
    pan: str | None = None           # CAS: first holder's PAN for the folio (drives profile matching)
    folio: str | None = None
    investor_name: str | None = None
    estimated: bool = False          # opening balance / summary holding — cost & date are estimates

    @property
    def fingerprint(self) -> str:
        basis = self.external_id or f"{self.symbol}|{self.txn_type}|{self.trade_date}|{self.quantity}|{self.price}"
        return "imp:" + hashlib.sha1(basis.encode()).hexdigest()[:24]

    @property
    def yahoo_symbol(self) -> str:
        if self.asset_type == "mutual_fund" or self.symbol.startswith("^") or "." in self.symbol:
            return self.symbol
        return f"{self.symbol}.{'BO' if (self.exchange or '').upper() == 'BSE' else 'NS'}"


def dec(v: Any) -> Decimal:
    if v is None:
        return Decimal(0)
    s = str(v).replace(",", "").replace("₹", "").strip()
    if not s or s in ("-", "--"):
        return Decimal(0)
    try:
        return Decimal(s)
    except InvalidOperation as exc:
        raise ValueError(f"Not a number: {v!r}") from exc


DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y", "%Y-%m-%dT%H:%M:%S", "%d-%m-%Y %H:%M:%S",
                "%d %b %Y, %I:%M %p", "%Y/%m/%d", "%m/%d/%Y")


def parse_date(v: str) -> date:
    s = str(v).strip()
    candidates = [s, s.split("T")[0], s.split(" ")[0], s.split(",")[0]]
    for cand in candidates:
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(cand, fmt).date()
            except ValueError:
                continue
    raise ValueError(f"Unrecognised date: {v!r}")


def read_csv(content: bytes) -> list[dict[str, str]]:
    text = content.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.DictReader(io.StringIO(text), dialect=dialect))
    # rows with more values than headers put the extras in a list under key None — ignore those
    return [{k.strip().lower(): (v or "").strip() for k, v in r.items() if isinstance(k, str) and not isinstance(v, list)} for r in rows]


def pdf_needs_password(content: bytes) -> bool:
    try:
        import fitz

        return bool(fitz.open(stream=content, filetype="pdf").needs_pass)
    except Exception:  # noqa: BLE001 — unreadable here: let the real reader report it
        return False


def _pdf_is_epf(content: bytes) -> bool:
    """An unprotected PDF whose first page says it is an EPFO passbook (CAS statements are password-protected)."""
    try:
        import fitz

        doc = fitz.open(stream=content, filetype="pdf")
        if doc.needs_pass:
            return False
        from .epf import looks_like_epf

        return looks_like_epf(doc[0].get_text() if doc.page_count else "")
    except Exception:  # noqa: BLE001
        return False


def detect_kind(filename: str, content: bytes) -> str:
    """What the file is, from its content (never from the broker's name): a CAS PDF, a tax P&L, a trade history,
    a holdings statement, an NPS statement — or "unknown" (the person maps its columns once)."""
    name = filename.lower()
    if name.endswith(".pdf") or content[:4] == b"%PDF":
        return "epf_passbook" if _pdf_is_epf(content) else "cas_pdf"
    from .holdings import _tables, looks_like_holdings
    from .nps import looks_like_nps
    from .taxpnl import looks_like_tax_pnl
    from .trades import find_table

    if looks_like_tax_pnl(content):  # capital gains / tax P&L (any broker): sale lots with buy + sell values
        return "tax_pnl"
    head = content[:2048].decode("utf-8-sig", errors="ignore").lower()
    if head.lstrip().startswith("{") and '"folios"' in content[:200_000].decode("utf-8", errors="ignore").lower():
        return "cas_json"  # casparser's JSON export of a CAS (`casparser statement.pdf -o cas.json`)
    if "trade_id" in head and "order_execution_time" in head:
        return "zerodha_tradebook"
    try:
        tables = _tables(content)
    except Exception:
        tables = []
    if tables and find_table(tables):  # a trade history from any broker (CSV or Excel)
        return "trades"
    if looks_like_holdings(content):
        return "holdings"
    if looks_like_nps(content):
        return "nps_statement"
    return "unknown"


def parse(kind: str, content: bytes, password: str | None = None) -> tuple[list[ParsedTxn], list[dict[str, Any]]]:
    from . import cas, trades, zerodha

    if kind == "zerodha_tradebook":
        return zerodha.parse(content)
    if kind == "cas_pdf":
        return cas.parse(content, password or "")
    if kind == "cas_json":
        import json

        return cas.parse_cas_data(json.loads(content.decode("utf-8-sig")))
    return trades.parse(content)  # "trades" (and the older "generic_csv")
