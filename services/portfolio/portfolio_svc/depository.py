"""NSDL / CDSL consolidated account statement (eCAS) → a holdings check across every broker.

The depositories' monthly eCAS lists, for each demat account (whichever broker it's with), every ISIN and how
many shares are in it — but not what they cost. So it is not imported as holdings (that would invent gains);
it is compared with what Aquilvyn has for the same person (matched by the holder's PAN):

* ``ok``       — same quantity
* ``differs``  — Aquilvyn has a different quantity (a missed trade, a bonus / split, a partial import)
* ``missing``  — in the demat account but not in Aquilvyn (import that broker's holdings or trades)
* ``extra``    — in Aquilvyn but in none of this person's demat accounts (sold, or recorded twice)

Mutual funds come from the CAMS / KFintech CAS, so only shares and ETFs are checked here.
"""
from __future__ import annotations

import io
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fm_common import http
from fm_common.config import settings
from fm_common.crypto import mask_pan, mask_tail, pan_fingerprint
from fm_common.deps import Principal
from fm_common.logging import get_logger

from . import access
from .holdings import load_txns, to_txn
from .ledger import build_positions
from .models import Portfolio, Profile

log = get_logger(__name__)
TOL = Decimal("0.001")


def read(content: bytes, password: str) -> dict[str, Any]:
    """casparser output as a dict — a CAMS/KFintech CAS has "folios", an NSDL/CDSL eCAS has "accounts"."""
    import casparser

    from .importers.cas import _to_dict

    return _to_dict(casparser.read_cas_pdf(io.BytesIO(content), password))


def is_depository(data: dict[str, Any]) -> bool:
    return bool(data.get("accounts")) and not data.get("folios")


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v).replace(",", ""))
    except Exception:
        return Decimal(0)


def reconcile(data: dict[str, Any], held: dict[str, dict[str, Decimal]], profile_of_pan: dict[str, dict[str, str]],
              names: dict[str, str] | None = None) -> dict[str, Any]:
    """``held``: {profile_id: {ISIN: quantity in Aquilvyn}} · ``profile_of_pan``: {PAN: {id, name}}."""
    accounts: list[dict[str, Any]] = []
    seen: dict[str, set[str]] = {}
    counts = {"ok": 0, "differs": 0, "missing": 0, "extra": 0}
    for a in data.get("accounts") or []:
        owners = a.get("owners") or []
        pan = next((str(o.get("pan") or "").strip().upper() for o in owners if o.get("pan")), "")
        prof = profile_of_pan.get(pan)
        mine = held.get(prof["id"], {}) if prof else {}
        lines = []
        for e in a.get("equities") or []:
            isin = str(e.get("isin") or "").upper()
            dq, ours = _d(e.get("num_shares")), mine.get(isin, Decimal(0))
            status = "missing" if ours == 0 else "ok" if abs(ours - dq) <= TOL else "differs"
            counts[status] += 1
            if prof:
                seen.setdefault(prof["id"], set()).add(isin)
            lines.append({"isin": isin, "name": e.get("name") or (names or {}).get(isin) or isin, "depository_qty": float(dq),
                          "our_qty": float(ours), "price": float(_d(e.get("price"))), "value": float(_d(e.get("value"))), "status": status})
        accounts.append({
            "broker": str(a.get("name") or "").title(), "type": a.get("type"), "client": mask_tail(str(a.get("client_id") or ""), 3),
            "holder": owners[0].get("name") if owners else None, "pan_masked": mask_pan(pan) if pan else None,
            "profile": prof, "value": float(_d(a.get("balance"))),
            "lines": sorted(lines, key=lambda x: ({"missing": 0, "differs": 1, "ok": 2}[x["status"]], -x["value"])),
        })
    extra: list[dict[str, Any]] = []
    for pid, isins in seen.items():
        for isin, q in held.get(pid, {}).items():
            if isin not in isins and q > 0:
                counts["extra"] += 1
                extra.append({"isin": isin, "name": (names or {}).get(isin) or isin, "our_qty": float(q), "status": "extra",
                              "profile": next((p for p in profile_of_pan.values() if p["id"] == pid), None)})
    unmatched = [a["pan_masked"] for a in accounts if a["profile"] is None and a["pan_masked"]]
    period = data.get("statement_period") or {}
    return {"accounts": accounts, "extra": extra, "counts": counts, "unmatched_pans": unmatched,
            "period": {"from": period.get("from") or period.get("from_"), "to": period.get("to")}}


async def check(db: AsyncSession, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
    visible = set(await access.visible_profile_ids(db, principal))
    profiles = [p for p in (await db.execute(select(Profile).where(Profile.household_id == principal.household_id,
                                                                  Profile.deleted_at.is_(None)))).scalars() if p.id in visible]
    profile_of_pan: dict[str, dict[str, str]] = {}
    for a in data.get("accounts") or []:
        for o in a.get("owners") or []:
            pan = str(o.get("pan") or "").strip().upper()
            if pan and pan not in profile_of_pan:
                hit = next((p for p in profiles if p.pan_hash and p.pan_hash == pan_fingerprint(pan)), None)
                if hit:
                    profile_of_pan[pan] = {"id": str(hit.id), "name": hit.display_name}
    # what Aquilvyn holds for each matched person, by ISIN
    held: dict[str, dict[str, Decimal]] = {}
    qty_by_inst: dict[str, dict[str, Decimal]] = {}
    for prof in {p["id"] for p in profile_of_pan.values()}:
        pfs = [pf.id for pf in (await db.execute(select(Portfolio).where(Portfolio.profile_id == uuid.UUID(prof),
                                                                         Portfolio.deleted_at.is_(None)))).scalars()]
        positions = build_positions([to_txn(t) for t in await load_txns(db, principal.household_id, pfs)])
        qty_by_inst[prof] = {iid: sum((lot.qty for lot in pos.lots), Decimal(0)) for iid, pos in positions.items()
                             if pos.asset_type in ("stock", "etf")}
    ids = sorted({iid for q in qty_by_inst.values() for iid in q})
    names: dict[str, str] = {}
    isin_of: dict[str, str] = {}
    if ids:
        try:
            insts = await http.post(settings.market_url, "/api/v1/market/instruments/batch", token=principal.token, json={"ids": ids})
            for i in insts:
                if i.get("isin"):
                    isin_of[i["id"]] = i["isin"]
                    names[i["isin"]] = i.get("name") or i.get("symbol")
        except Exception as exc:
            log.warning("depository.instruments_failed", error=str(exc)[:200])
    for prof, q in qty_by_inst.items():
        held[prof] = {}
        for iid, n in q.items():
            if iid in isin_of:
                held[prof][isin_of[iid]] = held[prof].get(isin_of[iid], Decimal(0)) + n
    return reconcile(data, held, profile_of_pan, names)
