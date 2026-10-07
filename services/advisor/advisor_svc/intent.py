"""Holding Intent Engine (E18): infer CORE / SATELLITE / TRADE / RETIREMENT when the user
hasn't said. The user's explicit choice always wins (intent_source = 'user')."""
from __future__ import annotations

from typing import Any

RETIREMENT_TYPES = {"epf", "vpf", "ppf", "nps"}


def infer(row: dict[str, Any], analysis: dict[str, Any] | None, trade_count: int = 0) -> tuple[str, str]:
    """Returns (intent, explanation)."""
    at = row["asset_type"]
    if at in RETIREMENT_TYPES:
        return "retirement", "Retirement account (EPF/VPF/PPF/NPS)"
    if at in ("fixed_deposit", "bond", "cash"):
        return "core", "Fixed-income holding"
    days = row.get("holding_days") or 0
    if at == "mutual_fund":
        return "core", "Mutual funds are long-term compounding vehicles by default"
    if at == "etf":
        return ("core" if "nifty" in (row.get("name") or "").lower() or "gold" in (row.get("name") or "").lower() else "satellite"), "Index/commodity ETF"
    fund = ((analysis or {}).get("fundamental") or {}).get("score")
    if row.get("dates_estimated"):
        # from a holdings statement: the purchase date is a guess (often "today"), so it can't make this a short-term trade
        if fund is not None and fund >= 60:
            return "core", f"Quality business (fundamental score {fund:.0f}); purchase date unknown (holdings statement)"
        return "satellite", "Purchase date unknown (holdings statement) — treated as a medium-term position until you say otherwise"
    if days < 90 and trade_count >= 3:
        return "trade", f"Held {days} days with {trade_count} trades — looks like a short-term position"
    if days < 45:
        return "trade", f"Recently bought ({days} days) — treated as short-term until you say otherwise"
    if fund is not None and fund >= 60:
        return "core", f"Quality business (fundamental score {fund:.0f}) held {days} days"
    return "satellite", "Medium-term position"
