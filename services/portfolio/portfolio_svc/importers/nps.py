"""NPS transaction statement (CRA / NSDL-Protean portal → "Transaction Statement" CSV).

Imported as current holdings, one per scheme (units, NAV, value from the "Scheme Wise Summary").
Invested = the statement's "Total Contribution" split across schemes in proportion to their value,
so the portfolio's total value, invested and gain match the statement exactly. The PRAN is used
to route the next statement to the same person.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .base import dec
from .holdings import HoldingRow


def looks_like_nps(content: bytes) -> bool:
    head = content[:3000].decode("utf-8-sig", errors="ignore").lower()
    return "nps transaction statement" in head or ("pran" in head and "pension fund" in content[:20000].decode("utf-8", errors="ignore").lower())


def _rows(content: bytes) -> list[list[str]]:
    return [[(c or "").strip() for c in r] for r in csv.reader(io.StringIO(content.decode("utf-8-sig", errors="replace")))]


def _money(v: str) -> Decimal:
    return dec(re.sub(r"(?i)rs\.?", "", v or ""))


def scheme_symbol(name: str) -> str:
    """'ICICI PENSION FUND SCHEME E - TIER I POP' → 'NPS-ICICI-E-T1'."""
    n = name.upper()
    pfm = re.sub(r"[^A-Z]", "", n.split()[0]) or "PFM"
    m = re.search(r"SCHEME\s+([ECGA])\b", n)
    cls = m.group(1) if m else "X"
    tier = "T2" if re.search(r"TIER\s*(II|2)\b", n) else "T1"
    return f"NPS-{pfm}-{cls}-{tier}"


def _nav_date(header: list[str]) -> date | None:
    m = re.search(r"NAV as on\s+(\d{1,2}-[A-Za-z]{3}-\d{4})", " ".join(header), re.I)
    try:
        return datetime.strptime(m.group(1), "%d-%b-%Y").date() if m else None
    except ValueError:
        return None


def statement_meta(content: bytes) -> dict[str, str]:
    for r in _rows(content)[:30]:
        if r and r[0].upper() == "PRAN" and len(r) > 1 and re.fullmatch(r"\d{8,14}", r[1]):
            return {"pran": r[1]}
    return {}


def parse(content: bytes) -> tuple[list[HoldingRow], list[dict[str, Any]]]:
    rows = _rows(content)
    errors: list[dict[str, Any]] = []
    total_contribution = None
    for i, r in enumerate(rows):  # "Investment Summary": header row, a letters row, then values
        if r and r[0].lower().startswith("value of your holdings"):
            for vals in rows[i + 1:i + 4]:
                if vals and vals[0].lower().startswith("rs"):
                    total_contribution = _money(vals[2]) if len(vals) > 2 else None
                    break
            break
    schemes: list[tuple[str, Decimal, Decimal, Decimal]] = []  # name, value, units, nav
    start = next((i for i, r in enumerate(rows) if r and r[0].lower() == "particulars" and len(r) > 2 and "units" in " ".join(r).lower()), None)
    nav_date = _nav_date(rows[start]) if start is not None else None
    if start is not None:
        for r in rows[start + 1:]:
            if not r or not r[0] or "scheme" not in r[0].lower():
                break
            try:
                schemes.append((r[0], _money(r[1]), dec(r[2]), dec(r[3])))
            except (ValueError, IndexError) as exc:
                errors.append({"row": r[0], "error": f"Could not read this scheme: {exc}"})
    if not schemes:
        return [], [{"row": "file", "error": "No 'Scheme Wise Summary' found in this NPS statement."}]
    total_value = sum((v for _, v, _, _ in schemes), Decimal(0))
    out: list[HoldingRow] = []
    for name, value, units, nav in schemes:
        if units <= 0:
            continue
        invested = (total_contribution * value / total_value).quantize(Decimal("0.01")) if total_contribution and total_value else value
        out.append(HoldingRow(
            name=re.sub(r"\s+POP$", "", name.strip()).title(), symbol=scheme_symbol(name), isin=None, asset_type="nps",
            quantity=units, avg_price=(invested / units).quantize(Decimal("0.0001")), invested=invested, price=nav, source_row=name, price_date=nav_date,
        ))
    if total_contribution:
        errors.append({"row": "statement", "level": "warning",
                       "error": "NPS: invested is your total contribution split across schemes by value (per-scheme contributions are not "
                                "on the statement); totals match the statement. Contribution history isn't imported yet."})
    return out, errors
