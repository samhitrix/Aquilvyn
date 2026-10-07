"""One source per mutual fund: a CAMS / KFintech CAS wins over a broker's holdings statement.

A broker holdings file (Zerodha Coin etc.) lists only the fund units held in *demat*, as one average-cost snapshot.
The CAS lists every folio of the fund — demat and non-demat — with the real purchase history. Imported side by
side they would count the demat units twice, and a later broker snapshot would replace the CAS history with
demat-only units. So, matched by ISIN or AMFI scheme code:

* CAS imported after a broker snapshot → the snapshot's rows for those funds are set aside (undo: delete the CAS
  import and they come back);
* broker snapshot imported after a CAS → funds the CAS already covers are left as they are (not added, not replaced).
"""
from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fm_common import http
from fm_common.config import settings as common
from fm_common.deps import Principal
from fm_common.logging import get_logger

from .models import ImportJob, Transaction

log = get_logger(__name__)
CAS_KINDS = ("cas_pdf", "cas_json")
SNAPSHOT_KINDS = ("holdings",)


def keys_of(code: str | None, isin: str | None) -> set[str]:
    out = set()
    if code:
        out.add(f"code:{str(code).strip().upper()}")
    if isin and len(str(isin).strip()) == 12:
        out.add(f"isin:{str(isin).strip().upper()}")
    return out


async def sources_of(db: AsyncSession, household_id: uuid.UUID, kinds: tuple[str, ...]) -> set[str]:
    """``source`` values ("import:<job id>") of the household's imports of these kinds."""
    jobs = (await db.execute(select(ImportJob.id).where(ImportJob.household_id == household_id, ImportJob.kind.in_(kinds)))).scalars()
    return {f"import:{j}" for j in jobs}


async def isins(principal: Principal, instrument_ids: set[uuid.UUID]) -> dict[str, str]:
    """instrument id → ISIN (from the market service; empty when it can't be reached — the scheme code still matches)."""
    if not instrument_ids:
        return {}
    try:
        rows = await http.post(common.market_url, "/api/v1/market/instruments/batch", token=principal.token,
                               json={"ids": [str(i) for i in instrument_ids]}) or []
    except (HTTPException, Exception) as exc:  # noqa: BLE001 — ISIN is a second key; the code alone still matches
        log.warning("fund_sources.isin_lookup_failed", error=str(exc)[:200])
        return {}
    return {str(r["id"]): str(r.get("isin") or "") for r in rows if r.get("id")}


async def fund_keys(principal: Principal, txns: list[Transaction]) -> dict[uuid.UUID, set[str]]:
    """Each fund transaction's match keys: its AMFI code (the symbol) and its ISIN."""
    by_inst = await isins(principal, {t.instrument_id for t in txns})
    return {t.id: keys_of(t.symbol, by_inst.get(str(t.instrument_id))) for t in txns}


def covered(keys: set[str], txn_keys: dict[uuid.UUID, set[str]], txns: list[Transaction]) -> list[Transaction]:
    return [t for t in txns if txn_keys.get(t.id, set()) & keys]


def summary(names: list[str], limit: int = 4) -> str:
    names = sorted(set(names))
    return ", ".join(names[:limit]) + (f" and {len(names) - limit} more" if len(names) > limit else "")
