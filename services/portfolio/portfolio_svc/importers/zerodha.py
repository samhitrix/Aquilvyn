"""Zerodha Console tradebook CSV:
symbol,isin,trade_date,exchange,segment,series,trade_type,auction,quantity,price,trade_id,order_id,order_execution_time"""
from __future__ import annotations

from typing import Any

from .base import ParsedTxn, dec, parse_date, read_csv


def parse(content: bytes) -> tuple[list[ParsedTxn], list[dict[str, Any]]]:
    out: list[ParsedTxn] = []
    errors: list[dict[str, Any]] = []
    for i, r in enumerate(read_csv(content), start=2):
        try:
            if r.get("segment", "EQ").upper() not in ("EQ", ""):
                errors.append({"row": i, "error": f"Segment {r.get('segment')} not supported in P1 (F&O arrives in P2)"})
                continue
            side = r["trade_type"].lower()
            if side not in ("buy", "sell"):
                raise ValueError(f"Unknown trade_type {side}")
            qty, price = dec(r["quantity"]), dec(r["price"])
            out.append(ParsedTxn(
                symbol=r["symbol"].upper(), asset_type="etf" if r["symbol"].upper().endswith("BEES") else "stock",
                txn_type=side, trade_date=parse_date(r["trade_date"]), quantity=qty, price=price, amount=qty * price,
                exchange=r.get("exchange") or "NSE", isin=r.get("isin") or None,
                external_id=f"zerodha:{r.get('trade_id')}:{r.get('order_id')}" if r.get("trade_id") else None, raw=r,
            ))
        except Exception as exc:
            errors.append({"row": i, "error": str(exc)})
    return out, errors
