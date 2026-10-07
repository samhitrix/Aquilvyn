"""CDSL consolidated account statement (eCAS) reader — the demat + mutual-fund statement CDSL emails monthly.

casparser (used for CAMS / KFintech) can't read CDSL's current layout ("Error parsing CAS header"), so this
reads the PDF's tables directly (PyMuPDF ``find_tables``) and recognises each by its column headings:

* holdings   — "HOLDING STATEMENT AS ON …": ISIN · Security · Current Bal / Free Bal · Market Price · Value
* funds      — "MUTUAL FUND UNITS HELD AS ON …": Scheme Name · ISIN · Folio · Closing Bal (Units) · NAV ·
               Cumulative Amount Invested · Valuation
* transactions (ISIN · Transaction Particulars · Op. Bal · Credit · Debit · Cl. Bal) and the lock-in
  "(Other Details)" table are not needed for the holdings check and are skipped.

Each holdings table belongs to the demat account (BO ID, 16 digits) printed above it. The output has the same
shape as casparser's NSDL / CDSL result, so ``depository.reconcile`` works on either.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

PAN_RE = re.compile(r"\b([A-Z]{5}[0-9]{4}[A-Z])\b")
BO_RE = re.compile(r"(?:BO\s*ID|Client\s*ID|Demat\s*(?:Account|A/c)\s*(?:No\.?|Number)?)\s*[:\-]?\s*(\d{16})", re.I)
BO_BARE = re.compile(r"\b(1\d{15})\b")  # CDSL BO IDs are 16 digits starting with 1
DP_NAME_RE = re.compile(r"DP\s*Name\s*[:\-]?\s*([A-Za-z0-9&.,()' \-]{3,80}?)(?:\s{2,}|\s+DP\s*I[dD]|\n|$)")
PERIOD_RE = re.compile(r"(?:from|period)\s*:?\s*(\d{2}[-/]\d{2}[-/]\d{4})\s*(?:to|-)\s*(\d{2}[-/]\d{2}[-/]\d{4})", re.I)
AS_ON_RE = re.compile(r"AS\s+ON\s+(\d{2}[-/]\d{2}[-/]\d{4})", re.I)


def is_cdsl(text: str) -> bool:
    t = text.lower()
    return "central depository services" in t or ("cdsl" in t and "consolidated account statement" in t)


def _num(v: Any) -> Decimal | None:
    s = re.sub(r"[^\d.\-]", "", str(v or "").replace(",", ""))
    if s in ("", "-", ".", "--"):
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _norm(h: Any) -> str:
    return " ".join(str(h or "").lower().replace("\n", " ").split())


def classify(header: list[str]) -> str | None:
    h = [_norm(c) for c in header]
    joined = " | ".join(h)
    if "isin" not in joined:
        return None
    if "transaction" in joined or "credit" in joined or "debit" in joined:
        return "transactions"
    if "lockin" in joined or "lock-in" in joined or "lock in" in joined or "balance description" in joined:
        return "lockin"
    if "scheme" in joined and ("folio" in joined or "nav" in joined):
        return "funds"
    if any("bal" in c for c in h) and any("value" in c or "price" in c for c in h):
        return "holdings"
    return None


def _col(header: list[str], *names: str) -> int | None:
    h = [_norm(c) for c in header]
    for n in names:  # a heading that starts with the word wins ("Value (₹)" over "Market Price / Face Value")
        for i, c in enumerate(h):
            if c.startswith(n):
                return i
    for n in names:
        for i, c in enumerate(h):
            if n in c:
                return i
    return None


def holdings_rows(header: list[str], rows: list[list[Any]]) -> list[dict[str, Any]]:
    i_isin, i_name = _col(header, "isin"), _col(header, "security", "company", "name")
    i_qty = _col(header, "current bal", "curr. bal", "free bal", "balance", "bal")
    i_price, i_value = _col(header, "market price", "price", "rate"), _col(header, "value")
    out = []
    for r in rows:
        isin = str(r[i_isin] if i_isin is not None and i_isin < len(r) else "").strip().upper().replace(" ", "")
        if not re.fullmatch(r"IN[A-Z0-9]{9}\d", isin):
            continue
        qty = _num(r[i_qty]) if i_qty is not None and i_qty < len(r) else None
        if not qty:
            continue
        out.append({"isin": isin, "name": " ".join(str(r[i_name] or "").split()) if i_name is not None else isin,
                    "num_shares": str(qty), "price": str(_num(r[i_price]) or 0) if i_price is not None else "0",
                    "value": str(_num(r[i_value]) or 0) if i_value is not None else "0"})
    return out


def fund_rows(header: list[str], rows: list[list[Any]]) -> list[dict[str, Any]]:
    i_name, i_isin, i_folio = _col(header, "scheme"), _col(header, "isin"), _col(header, "folio")
    i_units, i_nav = _col(header, "closing bal", "units", "balance"), _col(header, "nav")
    i_inv, i_val = _col(header, "cumulative amount invested", "amount invested", "invested"), _col(header, "valuation", "value")

    def cell(r: list[Any], i: int | None) -> Any:
        return r[i] if i is not None and i < len(r) else None

    out = []
    for r in rows:
        isin = str(cell(r, i_isin) or "").strip().upper().replace(" ", "")
        units = _num(cell(r, i_units))
        if not re.fullmatch(r"INF[A-Z0-9]{8}\d", isin) or not units:
            continue
        out.append({"isin": isin, "name": " ".join(str(cell(r, i_name) or "").split()), "folio": str(cell(r, i_folio) or "").strip(),
                    "units": str(units), "nav": str(_num(cell(r, i_nav)) or 0), "invested": str(_num(cell(r, i_inv)) or 0),
                    "value": str(_num(cell(r, i_val)) or 0)})
    return out


def _tables(page: Any) -> list[Any]:
    try:
        found = page.find_tables()
        tabs = list(found.tables)
        if not tabs:
            tabs = list(page.find_tables(strategy="text").tables)
        return tabs
    except Exception:  # noqa: BLE001 — a page we can't read just contributes nothing
        return []


def parse(content: bytes, password: str = "") -> dict[str, Any]:
    """→ {"file_type": "CDSL", "accounts": [{name, type, client_id, owners: [{name, pan}], balance, equities}],
          "mutual_funds": [...], "statement_period": {from, to}}"""
    import fitz  # PyMuPDF

    doc = fitz.open(stream=content, filetype="pdf")
    if doc.needs_pass and not doc.authenticate(password or ""):
        raise ValueError("incorrect password")
    full = "\n".join(p.get_text() for p in doc)
    pans = PAN_RE.findall(full)
    pan = next((p for p in pans if re.search(r"PAN\s*[:\-]?\s*" + p, full)), pans[0] if pans else "")
    period = PERIOD_RE.search(full)
    as_on = AS_ON_RE.search(full)

    # account markers in reading order: (page, y, bo_id, dp_name)
    markers: list[tuple[int, float, str, str]] = []
    for pno, page in enumerate(doc):
        for b in page.get_text("blocks"):
            text = b[4]
            m = BO_RE.search(text)
            if m:
                dp = DP_NAME_RE.search(text)
                markers.append((pno, b[1], m.group(1), dp.group(1).strip() if dp else ""))
    if not markers:  # BO ID not labelled: any 16-digit CDSL id outside a table row
        for pno, page in enumerate(doc):
            for b in page.get_text("blocks"):
                if (m := BO_BARE.search(b[4])) and "isin" not in b[4].lower() and not re.search(r"IN[EF][A-Z0-9]{9}", b[4]):
                    markers.append((pno, b[1], m.group(1), ""))
    dp_names = {bo: name for _, _, bo, name in markers if name}

    accounts: dict[str, dict[str, Any]] = {}
    funds: list[dict[str, Any]] = []
    last_kind_header: tuple[str, list[str]] | None = None
    for pno, page in enumerate(doc):
        for t in _tables(page):
            data = t.extract() or []
            if not data:
                continue
            header, rows = [str(c or "") for c in data[0]], data[1:]
            kind = classify(header)
            if kind is None and last_kind_header and len(header) == len(last_kind_header[1]):
                kind, rows = last_kind_header[0], data  # a table continued on the next page, without its header
                header = last_kind_header[1]
            if kind is None:
                continue
            last_kind_header = (kind, header)
            if kind == "funds":
                funds += fund_rows(header, rows)
            elif kind == "holdings":
                top = t.bbox[1]
                above = [m for m in markers if (m[0], m[1]) <= (pno, top)]
                bo = above[-1][2] if above else (markers[0][2] if markers else "")
                acct = accounts.setdefault(bo, {"name": dp_names.get(bo, ""), "type": "CDSL", "client_id": bo,
                                                "owners": [{"name": None, "pan": pan}] if pan else [], "balance": "0", "equities": []})
                acct["equities"] += holdings_rows(header, rows)
    for a in accounts.values():
        a["balance"] = str(sum((Decimal(e["value"]) for e in a["equities"]), Decimal(0)))
    to = period.group(2) if period else as_on.group(1) if as_on else None
    return {"file_type": "CDSL", "accounts": [a for a in accounts.values() if a["equities"]], "mutual_funds": funds,
            "owners": [{"name": None, "pan": pan}] if pan else [],
            "statement_period": {"from": period.group(1) if period else None, "to": to}}


def describe(content: bytes, password: str = "") -> list[str]:
    """For a 'could not read' report: each table's page, kind and column headings — no values, nothing personal."""
    import fitz

    doc = fitz.open(stream=content, filetype="pdf")
    if doc.needs_pass and not doc.authenticate(password or ""):
        return ["incorrect password"]
    out = []
    for pno, page in enumerate(doc):
        for t in _tables(page):
            data = t.extract() or []
            if data:
                header = [_norm(c) for c in data[0]]
                out.append(f"page {pno + 1}: {classify(header) or '?'} — " + " | ".join(header)[:200])
    return out or ["no tables found"]
