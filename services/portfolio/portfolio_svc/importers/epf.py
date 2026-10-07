"""EPFO member passbook (passbook.epfindia.gov.in → Download Passbook, one PDF per financial year).

Each passbook becomes ledger entries on the member's EPF account (Retirement portfolio):

* "OB Int. Updated upto 31/03/YYYY"  → the opening balance (employee + employer share), an *estimated*
  contribution on 1 April — it already includes earlier years' interest. When the previous year's passbook
  is imported too, its entries replace this stand-in (see ``imports.write_epf``).
* monthly "CR" rows ("Cont. for Due-Month 042026") → contributions (employee + employer share)
* "DR" rows (claims / withdrawals)                → withdrawals
* "Int. Updated upto 31/03/YYYY"                  → the year's interest credit (interest then accrues only after it)
* "Transfer In" rows                              → contributions

The pension (EPS) column is not part of the EPF balance — it is paid as a pension, not withdrawable — so it is
reported but not imported. The passbook's own "Closing Balance" is used to check the result.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date
from decimal import Decimal
from typing import Any

from .base import ParsedTxn, dec

NUM = r"-?[\d,]+(?:\.\d+)?"
ROW_RE = re.compile(r"^(?:(?P<wm>[A-Za-z]{3}-\d{4})\s+)?(?P<date>\d{2}[-/]\d{2}[-/]\d{4})\s+(?P<type>CR|DR)\s+(?P<part>.*?)\s+"
                    r"(?P<nums>(?:" + NUM + r"\s+){4}" + NUM + r")\s*$")
OB_RE = re.compile(r"^OB\s+Int\.?\s*Updated\s+up\s*to\s+(?P<upto>\d{2}/\d{2}/\d{4})\s+(?P<nums>(?:" + NUM + r"\s+){2}" + NUM + r")\s*$", re.I)
INT_RE = re.compile(r"^Int\.?\s*Updated\s+up\s*to\s+(?P<upto>\d{2}/\d{2}/\d{4})\s+(?P<nums>(?:" + NUM + r"\s+){2}" + NUM + r")\s*$", re.I)
CLOSE_RE = re.compile(r"Closing\s+Balance\s+as\s+on\s+(?P<on>\d{2}/\d{2}/\d{4})\s+(?P<nums>(?:" + NUM + r"\s+){2}" + NUM + r")", re.I)
MEMBER_RE = re.compile(r"Member\s*I[dD]\s*/?\s*Name\s*[:\-]?\s*([A-Z]{5}\d{10,}\d*)\s*/?\s*([A-Za-z .']+)?")
UAN_RE = re.compile(r"\bUAN\s*[:\-]?\s*(\d{12})\b")
EST_RE = re.compile(r"Establishment\s*I[dD]\s*/?\s*Name\s*[:\-]?\s*([A-Z]{5}\d{7,})\s*/?\s*([A-Za-z0-9 .,&()'\-]+)?")
FY_RE = re.compile(r"Financial\s+Year\s*[-:]?\s*(\d{4})\s*-\s*(\d{4})", re.I)
MEMBER_BARE = re.compile(r"\b([A-Z]{5}\d{17})\b")  # 5 letters + 17 digits (establishment code + member serial)
DEFAULT_RATE = 8.25


def looks_like_epf(text: str) -> bool:
    t = text.lower()
    return ("epf passbook" in t or "पासबुक" in text) and ("employee" in t or "member" in t)


def _d(s: str) -> date:
    dd, mm, yy = re.split(r"[-/]", s)
    return date(int(yy), int(mm), int(dd))


def _nums(s: str) -> list[Decimal]:
    return [dec(x) for x in s.split()]


def lines(content: bytes) -> list[str]:
    """The PDF's text as visual lines (words on the same baseline joined left to right)."""
    import fitz

    doc = fitz.open(stream=content, filetype="pdf")
    out: list[str] = []
    for page in doc:
        rows: dict[int, list[tuple[float, str]]] = {}
        for x0, y0, _x1, y1, word, *_ in page.get_text("words"):
            key = round((y0 + y1) / 2 / 3)  # ~3pt bands
            band = rows.get(key) or rows.get(key - 1) or rows.get(key + 1)
            (band if band is not None else rows.setdefault(key, [])).append((x0, word))
        for key in sorted(rows):
            out.append(" ".join(w for _, w in sorted(rows[key])))
    return out


def account_symbol(member_id: str) -> str:
    return "EPF-" + hashlib.sha1(member_id.encode()).hexdigest()[:10].upper()


def parse(content: bytes) -> tuple[list[ParsedTxn], list[dict[str, Any]], dict[str, Any]]:
    """→ (entries, issues, meta: {member_id, uan, name, establishment, fy, pension, closing, symbol})"""
    import fitz

    text = "\n".join(p.get_text() for p in fitz.open(stream=content, filetype="pdf"))
    ls = lines(content)
    joined = "\n".join(ls)
    m = MEMBER_RE.search(joined) or MEMBER_RE.search(text)
    member = m.group(1) if m else (MEMBER_BARE.search(joined).group(1) if MEMBER_BARE.search(joined) else "")
    name = " ".join((m.group(2) or "").split()).title() if m and m.group(2) else None
    est = EST_RE.search(joined) or EST_RE.search(text)
    est_name = " ".join((est.group(2) or "").split()) if est and est.group(2) else None
    uan = UAN_RE.search(joined)
    fy = FY_RE.search(joined) or FY_RE.search(text)
    issues: list[dict[str, Any]] = []
    if not member:
        issues.append({"row": "file", "error": "No Member ID found — is this an EPFO member passbook?"})
        return [], issues, {}
    sym = account_symbol(member)
    label = f"EPF{' · ' + est_name if est_name else ''} (Member ID …{member[-4:]})"
    rows: list[ParsedTxn] = []
    pension = Decimal(0)
    closing: dict[str, Any] | None = None

    def entry(kind: str, when: date, amount: Decimal, ref: str, estimated: bool = False, part: str = "") -> None:
        rows.append(ParsedTxn(symbol=sym, asset_type="epf", txn_type=kind, trade_date=when, quantity=amount, price=Decimal(1),
                              amount=amount, exchange=None, name=label, external_id=f"epf:{member}:{ref}",
                              raw={"particulars": part, "fy_start": fy_start.isoformat() if fy_start else None}, estimated=estimated))

    fy_start = date(int(fy.group(1)), 4, 1) if fy else None
    for i, line in enumerate(ls):
        s = " ".join(line.split())
        if mo := OB_RE.match(s):
            emp, er, pen = _nums(mo.group("nums"))
            upto = _d(mo.group("upto"))
            fy_start = fy_start or date(upto.year, 4, 1)
            pension += pen
            if emp + er > 0:
                entry("contribution", fy_start, emp + er, f"ob:{fy_start.year}", estimated=True, part="Opening balance (incl. earlier interest)")
            continue
        if mo := INT_RE.match(s):
            emp, er, pen = _nums(mo.group("nums"))
            if emp + er > 0:
                entry("interest", _d(mo.group("upto")), emp + er, f"int:{mo.group('upto')}", part="Interest")
            continue
        if mo := CLOSE_RE.search(s):
            emp, er, pen = _nums(mo.group("nums"))
            closing = {"on": _d(mo.group("on")).isoformat(), "employee": float(emp), "employer": float(er), "pension": float(pen)}
            continue
        if mo := ROW_RE.match(s):
            _wages, _eps_wages, emp, er, pen = _nums(mo.group("nums"))
            when, part = _d(mo.group("date")), mo.group("part").strip()
            pension += pen if mo.group("type") == "CR" else -pen
            amt = emp + er
            if amt <= 0:
                continue
            kind = "contribution" if mo.group("type") == "CR" else "withdrawal"
            entry(kind, when, amt, f"{when.isoformat()}:{mo.group('type')}:{part}:{i}", part=part)
    if not rows:
        issues.append({"row": "file", "error": "No passbook entries found (opening balance or monthly contributions)."})
    # check against the passbook's own closing balance
    if closing and rows:
        total = sum((r.amount if r.txn_type != "withdrawal" else -r.amount) for r in rows)
        want = Decimal(str(closing["employee"])) + Decimal(str(closing["employer"]))
        if abs(total - want) > 1:
            issues.append({"row": "check", "level": "warning",
                           "error": f"Entries add up to ₹{total:,.0f} but the passbook's closing balance is ₹{want:,.0f} — some rows may not have been read."})
    meta = {"member_id": member, "uan": uan.group(1) if uan else None, "name": name, "establishment": est_name,
            "fy": f"{fy.group(1)}-{fy.group(2)}" if fy else (f"{fy_start.year}-{fy_start.year + 1}" if fy_start else None),
            "pension": float(pension), "closing": closing, "symbol": sym, "label": label, "rate": DEFAULT_RATE}
    return rows, issues, meta
