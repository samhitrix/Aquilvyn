""""Map the columns once" — for a broker file whose layout FolioSense doesn't recognise yet.

``candidate`` finds the most likely header row and shows it with a few sample rows and a best guess per
field; the person confirms which column is which. ``apply`` renames those columns to the names the normal
readers understand and runs them, so a mapped file goes through exactly the same checks as a recognised one.
The mapping is saved per household under a fingerprint of the header row (column names only — never values),
so the next file with that layout imports without asking.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from . import holdings as hparser
from . import trades as tparser

FIELDS: dict[str, list[dict[str, Any]]] = {
    "trades": [
        {"key": "date", "label": "Trade date", "required": True},
        {"key": "side", "label": "Buy / Sell", "required": True},
        {"key": "symbol", "label": "Stock name / symbol", "required": False},
        {"key": "isin", "label": "ISIN", "required": False},
        {"key": "qty", "label": "Quantity", "required": True},
        {"key": "price", "label": "Price per share", "required": False},
        {"key": "amount", "label": "Trade value", "required": False},
        {"key": "fees", "label": "Charges / brokerage", "required": False},
        {"key": "exchange", "label": "Exchange", "required": False},
        {"key": "trade_id", "label": "Trade / order number", "required": False},
    ],
    "holdings": [
        {"key": "symbol", "label": "Stock name / symbol", "required": False},
        {"key": "isin", "label": "ISIN", "required": False},
        {"key": "qty", "label": "Quantity", "required": True},
        {"key": "avg", "label": "Average buy price", "required": False},
        {"key": "invested", "label": "Invested value", "required": False},
        {"key": "price", "label": "Current price", "required": False},
    ],
}
# at least one of each group must be mapped
ONE_OF = {"trades": [("symbol", "isin"), ("price", "amount")], "holdings": [("symbol", "isin"), ("avg", "invested")]}
CANON = {"trades": {k: v[0] for k, v in tparser.COLS.items()}, "holdings": {k: v[0] for k, v in hparser.COLS.items()}}


def fingerprint(headers: list[Any]) -> str:
    return hashlib.sha1("|".join(tparser.norm(h) for h in headers).encode()).hexdigest()


def _texty(row: list[Any]) -> int:
    return sum(1 for c in row if isinstance(c, str) and c.strip() and not c.strip().replace(".", "").replace(",", "").isdigit())


def header_row(tables: list[tuple[str, list[list[Any]]]]) -> tuple[str, int, list[Any]] | None:
    """The row with the most text cells that is followed by data rows — the table's header."""
    best: tuple[int, str, int] | None = None
    for sheet, rows in tables:
        for i, r in enumerate(rows[:60]):
            n = _texty(r)
            data_below = sum(1 for rr in rows[i + 1:i + 4] if sum(1 for c in rr if c not in (None, "")) >= max(2, n // 2))
            if n >= 3 and data_below and (best is None or n > best[0]):
                best = (n, sheet, i)
    if best is None:
        return None
    _, sheet, i = best
    return sheet, i, dict(tables)[sheet][i]


def _guess(kind: str, headers: list[Any]) -> dict[str, str]:
    """Best guess per field: exact alias first, then a header containing the alias ("Net Qty" ⊃ "qty")."""
    cols = tparser.COLS if kind == "trades" else hparser.COLS
    names = [tparser.norm(h) for h in headers]
    out: dict[str, str] = {}
    used: set[int] = set()
    for f in FIELDS[kind]:
        aliases = cols.get(f["key"], ())
        idx = next((i for a in aliases for i, n in enumerate(names) if n == a and i not in used), None)
        if idx is None:
            idx = next((i for a in aliases if len(a) > 2 for i, n in enumerate(names) if a in n and i not in used), None)
        if idx is not None:
            used.add(idx)
            out[f["key"]] = str(headers[idx])
    return out


ISIN = re.compile(r"^IN[EF9][0-9A-Z]{8}[0-9]$")


def _profile(values: list[str]) -> dict[str, bool]:
    """What a column's values look like — used to guess columns whose names say nothing ("B-S", "Nos", "Px")."""
    v = [x.strip() for x in values if x and x.strip()]
    if not v:
        return {}

    def all_(pred: Any) -> bool:
        return all(pred(x) for x in v)

    def is_date(x: str) -> bool:
        try:
            tparser._date(x)
            return True
        except Exception:
            return False

    def num(x: str) -> float | None:
        try:
            return float(x.replace(",", "").replace("₹", ""))
        except ValueError:
            return None
    nums = [num(x) for x in v]
    return {
        "side": all_(lambda x: x.lower() in tparser.SIDE),
        "isin": all_(lambda x: bool(ISIN.match(x.upper()))),
        "date": all_(is_date) and not all(n is not None for n in nums),
        "int": all(n is not None and float(n).is_integer() and n > 0 for n in nums) and not any("." in x for x in v),
        "decimal": all(n is not None for n in nums) and any("." in x or not float(n or 0).is_integer() for x, n in zip(v, nums, strict=True)),
        "text": all(n is None for n in nums) and any(" " in x or len(x) > 3 for x in v),
    }


def _guess_by_values(kind: str, headers: list[Any], rows: list[list[Any]], guessed: dict[str, str]) -> dict[str, str]:
    out = dict(guessed)
    used = set(out.values())
    cols = {j: _profile([str(r[j]) if j < len(r) and r[j] is not None else "" for r in rows]) for j in range(len(headers))}

    def take(field: str, test: str) -> None:
        if field in out:
            return
        j = next((j for j, p in cols.items() if p.get(test) and str(headers[j]).strip() and str(headers[j]).strip() not in used), None)
        if j is not None:
            out[field] = str(headers[j]).strip()
            used.add(out[field])
    take("isin", "isin")
    if kind == "trades":
        take("side", "side")
        take("date", "date")
    take("symbol", "text")
    take("qty", "int")
    take("price" if kind == "trades" else "avg", "decimal")
    return out


def candidate(content: bytes) -> dict[str, Any] | None:
    tables = hparser._tables(content)
    hit = header_row(tables)
    if hit is None:
        return None
    sheet, i, hdr = hit
    headers = [str(h).strip() if h not in (None, "") else "" for h in hdr]
    rows = dict(tables)[sheet]
    data = [r for r in rows[i + 1:i + 21] if any(c not in (None, "") for c in r)]
    samples = [[("" if c is None else str(c))[:40] for c in r] for r in data[:3]]
    has_side = any(tparser.norm(h) in tparser.COLS["side"] for h in headers) or any(
        _profile([str(r[j]) if j < len(r) and r[j] is not None else "" for r in data]).get("side") for j in range(len(headers)))
    kind = "trades" if has_side else "holdings"
    suggested = {k: _guess_by_values(k, headers, data, _guess(k, headers)) for k in FIELDS}
    return {"sheet": sheet, "row": i + 1, "headers": headers, "samples": samples, "fingerprint": fingerprint(headers), "kind": kind,
            "suggested": suggested, "fields": FIELDS, "one_of": ONE_OF}


def validate(kind: str, mapping: dict[str, str]) -> list[str]:
    problems = [f"“{f['label']}” is needed" for f in FIELDS[kind] if f["required"] and not mapping.get(f["key"])]
    for group in ONE_OF[kind]:
        if not any(mapping.get(k) for k in group):
            labels = [f["label"] for f in FIELDS[kind] if f["key"] in group]
            problems.append(f"pick at least one of: {' or '.join(labels)}")
    return problems


def apply(content: bytes, kind: str, mapping: dict[str, str]) -> tuple[list[Any], list[dict[str, Any]]]:
    """Rename the mapped columns to the names the readers know, drop everything else, and parse."""
    problems = validate(kind, mapping)
    if problems:
        raise ValueError("; ".join(problems))
    tables = hparser._tables(content)
    hit = header_row(tables)
    if hit is None:
        raise ValueError("no table found in this file")
    sheet, i, hdr = hit
    by_header = {str(h).strip(): j for j, h in enumerate(hdr) if h not in (None, "")}
    new_header: list[Any] = [None] * len(hdr)
    for field, header in mapping.items():
        if header and header in by_header:
            new_header[by_header[header]] = CANON[kind][field]
    missing = [h for h in mapping.values() if h and h not in by_header]
    if missing:
        raise ValueError(f"column(s) not in this file: {', '.join(missing)}")
    table = [new_header] + dict(tables)[sheet][i + 1:]
    if kind == "trades":
        return tparser.parse_tables([(sheet, table)])
    return hparser.parse_tables([(sheet, table)])
