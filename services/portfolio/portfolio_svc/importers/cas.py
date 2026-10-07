"""CAMS / KFintech Consolidated Account Statement (CAS PDF, password = PAN in caps, or PAN+DOB
for some statements) via the open-source ``casparser`` library.

* **Detailed** CAS → every transaction. If the statement period doesn't start at inception,
  each scheme's *opening balance* becomes one estimated "opening balance" buy so units match.
* **Summary** CAS → balances only (no transactions): one estimated holding per scheme at the
  valuation date, with a warning (tax lots / XIRR are approximate — download *Detailed*).
* Tax rows (stamp duty, STT, TDS) are not transactions: stamp duty is folded into the matching
  purchase's charges so cost stays exact; the others are skipped.

``parse_cas_data`` works on casparser's dict output, so the mapping is unit-testable without a PDF.
NSDL/CDSL demat CAS support is scheduled for P2."""
from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from typing import Any

from .base import ParsedTxn, parse_date

TYPE_MAP = {
    "PURCHASE": "buy", "PURCHASE_SIP": "sip", "SWITCH_IN": "switch_in", "SWITCH_IN_MERGER": "switch_in",
    "REDEMPTION": "sell", "SWITCH_OUT": "switch_out", "SWITCH_OUT_MERGER": "switch_out",
    "DIVIDEND_PAYOUT": "dividend", "DIVIDEND_REINVEST": "buy",
}
BUY_TYPES = {"PURCHASE", "PURCHASE_SIP", "SWITCH_IN", "SWITCH_IN_MERGER", "DIVIDEND_REINVEST"}
SELL_TYPES = {"REDEMPTION", "SWITCH_OUT", "SWITCH_OUT_MERGER"}
SILENT_SKIP = {"STT_TAX", "TDS_TAX", "MISC", "SEGREGATION"}
STAMP_DUTY = "STAMP_DUTY_TAX"
ZERO = Decimal(0)


def _enum_value(v: Any) -> str:
    """casparser gives ``TransactionType.PURCHASE`` (an Enum) for nested rows; str() of that is
    'TransactionType.PURCHASE', so read ``.value``."""
    return str(getattr(v, "value", v) or "").strip().upper()


def _d(v: Any) -> Decimal:
    return Decimal(str(v)) if v not in (None, "") else ZERO


def _date(v: Any) -> date:
    return v if isinstance(v, date) else parse_date(str(v))


def _to_dict(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        return data
    if hasattr(data, "model_dump"):
        return data.model_dump(by_alias=True)
    return dict(data)


def parse(content: bytes, password: str) -> tuple[list[ParsedTxn], list[dict[str, Any]]]:
    import casparser

    try:
        data = casparser.read_cas_pdf(io.BytesIO(content), password)
    except Exception as exc:
        return [], [{"row": "file", "error": f"Could not open CAS PDF (password is your PAN in capitals): {exc}"}]
    return parse_cas_data(_to_dict(data))


def parse_cas_data(data: dict[str, Any]) -> tuple[list[ParsedTxn], list[dict[str, Any]]]:
    """casparser output → ledger rows + errors/warnings (warnings carry ``level='warning'``)."""
    cas_type = _enum_value(data.get("cas_type")) or "DETAILED"
    period = data.get("statement_period") or {}
    period_from = period.get("from") or period.get("from_")
    investor = (data.get("investor_info") or {}).get("name")
    out: list[ParsedTxn] = []
    errors: list[dict[str, Any]] = []
    opening_estimates = 0
    summary_schemes = 0

    for folio in data.get("folios", []):
        folio_no = str(folio.get("folio") or "").strip()
        pan = (folio.get("PAN") or folio.get("pan") or "").strip().upper() or None
        for scheme in folio.get("schemes", []):
            name = scheme.get("scheme") or "Unknown scheme"
            code = str(scheme.get("amfi") or "").strip()
            if not code:
                errors.append({"row": name, "error": "No AMFI scheme code in the statement — add this fund manually"})
                continue

            def row(txn_type: str, when: date, units: Decimal, price: Decimal, amount: Decimal, ext: str, *, estimated: bool = False,
                    _code: str = code, _name: str = name, _isin: Any = scheme.get("isin"), _folio: str = folio_no, _pan: str | None = pan) -> ParsedTxn:
                return ParsedTxn(
                    symbol=_code, asset_type="mutual_fund", txn_type=txn_type, trade_date=when, quantity=units, price=price,
                    amount=amount, exchange="AMFI", isin=_isin, name=_name, external_id=f"cas:{_folio}:{_code}:{ext}",
                    pan=_pan, folio=_folio, investor_name=investor, estimated=estimated,
                )

            valuation = scheme.get("valuation") or {}
            txns = scheme.get("transactions") or []
            open_units = _d(scheme.get("open"))
            close_units = _d(scheme.get("close"))

            # ---- Summary CAS: balances only → one estimated holding per scheme
            if cas_type == "SUMMARY":
                if close_units <= 0:
                    continue
                when = _date(valuation.get("date"))
                cost = _d(valuation.get("cost"))
                price = (cost / close_units) if cost > 0 else _d(valuation.get("nav"))
                out.append(row("buy", when, close_units, price.quantize(Decimal("0.0001")), (price * close_units).quantize(Decimal("0.01")),
                               f"summary:{when}:{close_units}", estimated=True))
                summary_schemes += 1
                continue

            # ---- Detailed CAS
            scheme_rows: list[ParsedTxn] = []
            period_buys = ZERO
            has_sells = False
            for t in txns:
                ttype = _enum_value(t.get("type"))
                try:
                    when = _date(t.get("date"))
                except ValueError as exc:
                    errors.append({"row": f"{name} · {t.get('date')}", "error": str(exc)})
                    continue
                label = f"{name} · {when.isoformat()}"
                amount = abs(_d(t.get("amount")))
                units = abs(_d(t.get("units")))
                if ttype == STAMP_DUTY:
                    # fold into the latest purchase on the same day → exact cost basis
                    target = next((r for r in reversed(scheme_rows) if r.trade_date == when and r.txn_type in ("buy", "sip", "switch_in")), None)
                    if target is not None:
                        target.fees += amount
                    continue
                if ttype in SILENT_SKIP:
                    continue
                if ttype == "REVERSAL":
                    errors.append({"row": label, "level": "warning", "error": f"Reversal skipped ({t.get('description') or 'reversed entry'}) — check this scheme's units"})
                    continue
                mapped = TYPE_MAP.get(ttype)
                if not mapped:
                    errors.append({"row": label, "error": f"Unsupported CAS transaction '{t.get('description') or ttype}'"})
                    continue
                if mapped == "dividend":
                    if amount > 0:
                        scheme_rows.append(row("dividend", when, ZERO, ZERO, amount, f"{when}:{ttype}:{amount}"))
                    continue
                if units == 0:
                    continue
                nav = _d(t.get("nav")) or (amount / units)
                if ttype in BUY_TYPES:
                    period_buys += amount
                if ttype in SELL_TYPES:
                    has_sells = True
                scheme_rows.append(row(mapped, when, units, nav, amount, f"{when}:{ttype}:{units}:{amount}"))

            # opening balance: the statement started after the first purchase
            if open_units > 0:
                try:
                    start = _date(period_from) if period_from else min((r.trade_date for r in scheme_rows), default=_date(valuation.get("date")))
                except ValueError:
                    start = min((r.trade_date for r in scheme_rows), default=date.today())
                cost = _d(valuation.get("cost"))
                est = (cost - period_buys) / open_units if cost > 0 and not has_sells and cost > period_buys else ZERO
                if est <= 0:  # fall back to the first NAV seen in the period, else today's valuation NAV
                    first_nav = next((r.price for r in scheme_rows if r.price > 0), ZERO)
                    est = first_nav or _d(valuation.get("nav"))
                est = est.quantize(Decimal("0.0001"))
                scheme_rows.insert(0, row("buy", start, open_units, est, (est * open_units).quantize(Decimal("0.01")),
                                          f"opening:{start}:{open_units}", estimated=True))
                opening_estimates += 1
            out.extend(scheme_rows)

    if summary_schemes:
        errors.append({"row": "statement", "level": "warning",
                       "error": f"This is a Summary CAS: {summary_schemes} scheme(s) imported as holdings at cost without purchase dates, "
                                "so tax lots and XIRR are approximate. For exact results download the Detailed CAS from CAMS."})
    if opening_estimates:
        errors.append({"row": "statement", "level": "warning",
                       "error": f"{opening_estimates} scheme(s) had units before this statement's start date; added as an estimated "
                                "opening balance. Import a statement from inception (period 'since inception') for exact tax lots."})
    return out, errors
