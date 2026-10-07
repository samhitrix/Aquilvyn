"""Is a SIP still running in this fund? One rule, used by Holdings (the "SIP" badge) and the advisor (fund plan).

Read from the purchase dates (a CAMS / KFintech CAS has every one):
* a purchase in the last ~45 days (last month's instalment), or
* a regular monthly pattern — 3+ purchases about a month apart — whose latest is within ~70 days (one late or
  skipped instalment doesn't end a SIP).

Holdings imported from a broker's holdings file have no purchase dates (they are estimated): then the answer is
*unknown* (``active`` None), not "no" — the person can say yes / no on the holding (it overrides this guess).
"""
from __future__ import annotations

from datetime import date
from typing import Any

RECENT_DAYS = 45
GRACE_DAYS = 70


def _dates(lots: list[dict[str, Any]], today: date) -> list[date]:
    out = set()
    for lot in lots or []:
        try:
            d = date.fromisoformat(str(lot["buy_date"])[:10])
        except (KeyError, ValueError):
            continue
        if d <= today:
            out.add(d)
    return sorted(out)


def sip_info(lots: list[dict[str, Any]], today: date, *, estimated: bool = False) -> dict[str, Any]:
    """→ {active (True / False / None = unknown), last_buy (ISO date or None), monthly, source ("dates" | "unknown")}"""
    if estimated:
        return {"active": None, "last_buy": None, "monthly": False, "source": "unknown"}
    dates = _dates(lots, today)
    if not dates:
        return {"active": False, "last_buy": None, "monthly": False, "source": "dates"}
    last = (today - dates[-1]).days
    gaps = [(b - a).days for a, b in zip(dates[-4:], dates[-3:], strict=False)]  # the last three gaps
    monthly = len(dates) >= 3 and sum(1 for g in gaps if 20 <= g <= 40) >= 2
    active = last <= RECENT_DAYS or (last <= GRACE_DAYS and monthly)
    return {"active": active, "last_buy": dates[-1].isoformat(), "monthly": monthly, "source": "dates"}


def sip_active(lots: list[dict[str, Any]], today: date, *, estimated: bool = False) -> bool:
    return bool(sip_info(lots, today, estimated=estimated)["active"])
