"""Import Engine (E07) pipeline: parse → group by owner → match profiles → (review) → write.

A file can belong to one or more family members:
* CAMS/KFintech CAS — one group per PAN printed on the folios; matched via ``profiles.pan_hash``.
* Zerodha holdings statement — the Client ID at the top; matched via ``profile_accounts``.
* Anything else — one group; the person picks the profile.

``preview`` parses and stores the rows in Redis for 15 minutes under a token (the file itself and
any PDF password are never stored); ``commit`` applies the chosen owner per group, refusing to put
one person's PAN into another person's profile, and tags untagged profiles for next time.
"""
from __future__ import annotations

import dataclasses
import secrets
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import orjson
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fm_common import http
from fm_common.config import settings
from fm_common.crypto import account_fingerprint, decrypt, encrypt, mask_pan, mask_tail, pan_fingerprint
from fm_common.deps import Principal
from fm_common.events import publish
from fm_common.logging import get_logger
from fm_common.redis import get_redis

from . import access, importers, service, taxstore
from .holdings import load_txns, to_txn
from .importers import holdings as hparser
from .importers.base import ParsedTxn
from .importers.holdings import HoldingRow
from .importers.taxpnl import TaxRow
from .ledger import build_positions
from .models import ImportJob, Portfolio, PortfolioKind, Profile, ProfileAccount, Relationship, Transaction, TxnType

log = get_logger(__name__)
TTL_SECONDS = 15 * 60
MAX_BYTES = 15 * 1024 * 1024
KIND_LABEL = {"cas_pdf": "CAMS / KFintech CAS", "cas_json": "CAS (casparser JSON)", "holdings": "Holdings statement", "zerodha_tradebook": "Zerodha tradebook",
              "generic_csv": "Broker trade history (CSV)", "trades": "Trade history (any broker)",
              "unknown": "Unrecognised file", "demat_cas": "NSDL / CDSL depository statement (eCAS)",
              "nps_statement": "NPS transaction statement",
              "tax_pnl": "Tax P&L / capital gains statement"}


# ------------------------------------------------------------------ (de)serialisation
def _enc(v: Any) -> Any:
    if isinstance(v, Decimal):
        return {"$d": str(v)}
    if isinstance(v, date):
        return {"$t": v.isoformat()}
    return v


def _dec(v: Any) -> Any:
    if isinstance(v, dict) and "$d" in v:
        return Decimal(v["$d"])
    if isinstance(v, dict) and "$t" in v:
        return date.fromisoformat(v["$t"])
    return v


def _dump_rows(rows: list[Any]) -> list[dict[str, Any]]:
    return [{f.name: _enc(getattr(r, f.name)) for f in dataclasses.fields(r)} for r in rows]


def _load_rows(cls: type, rows: list[dict[str, Any]]) -> list[Any]:
    return [cls(**{k: _dec(v) for k, v in r.items()}) for r in rows]


def _key(token: str) -> str:
    return f"fm:import:{token}"


# ------------------------------------------------------------------ preview
async def _self_profile(db: AsyncSession, principal: Principal) -> Profile | None:
    return await db.scalar(select(Profile).where(Profile.household_id == principal.household_id, Profile.linked_user_id == principal.user_id,
                                                 Profile.deleted_at.is_(None)))


def _group_summary(kind: str, rows: list[Any]) -> dict[str, Any]:
    if kind == "tax_pnl":
        gains = [r for r in rows if r.record == "gain"]
        income = [r for r in rows if r.record == "income"]
        sells = [r.sell_date for r in gains if r.sell_date]
        return {"rows": len(rows), "sales": len(gains), "income_rows": len(income),
                "realised": float(sum((r.taxable_profit for r in gains), Decimal(0))),
                "dividends": float(sum((r.profit for r in income if r.term == "dividend"), Decimal(0))),
                "interest": float(sum((r.profit for r in income if r.term == "interest"), Decimal(0))),
                "from": min(sells).isoformat() if sells else None, "to": max(sells).isoformat() if sells else None,
                "zero_cost": sum("zero_cost" in r.flags for r in gains)}
    if kind in ("holdings", "nps_statement"):
        return {"rows": len(rows), "stocks": sum(r.asset_type not in ("mutual_fund", "nps") for r in rows),
                "funds": sum(r.asset_type in ("mutual_fund", "nps") for r in rows),
                "invested": float(sum((r.invested for r in rows), Decimal(0)))}
    dates = [r.trade_date for r in rows]
    return {"rows": len(rows), "instruments": len({r.symbol for r in rows}), "from": min(dates).isoformat() if dates else None,
            "to": max(dates).isoformat() if dates else None, "estimated": sum(bool(r.estimated) for r in rows)}


async def _mapped(db: AsyncSession, principal: Principal, filename: str, content: bytes, mapping: dict[str, Any] | None) -> tuple[str, list[Any], list[dict[str, Any]]]:
    """An unrecognised layout: use the mapping the person just confirmed (and remember it), or one they confirmed
    before for the same header row; otherwise ask them to map it (422 with the columns and sample rows)."""
    from .importers import mapping as mapmod
    from .models import ImportTemplate

    cand = mapmod.candidate(content)
    if cand is None:
        raise HTTPException(422, "No table found in this file — is it a holdings, trades or tax P&L report exported as CSV / Excel?")
    tmpl = await db.scalar(select(ImportTemplate).where(ImportTemplate.household_id == principal.household_id,
                                                        ImportTemplate.fingerprint == cand["fingerprint"]))
    if mapping:
        kind, fields = str(mapping.get("kind") or cand["kind"]), {k: v for k, v in (mapping.get("fields") or {}).items() if v}
    elif tmpl is not None:
        kind, fields = tmpl.kind, dict(tmpl.mapping)
    else:
        raise HTTPException(422, {"message": "Aquilvyn doesn't know this file's layout yet — tell it which column is which (once).",
                                  "needs_mapping": cand})
    if kind not in mapmod.FIELDS:
        raise HTTPException(422, "Choose whether the file is a trade history or a holdings statement")
    try:
        rows, issues = mapmod.apply(content, kind, fields)
    except ValueError as exc:
        raise HTTPException(422, {"message": f"That mapping doesn't work: {exc}", "needs_mapping": cand}) from exc
    if not rows:
        raise HTTPException(422, {"message": "; ".join(str(i["error"]) for i in issues[:3]) or "No rows could be read with that mapping.",
                                  "needs_mapping": cand})
    if mapping:  # it worked: remember it for this layout
        label = str(mapping.get("label") or "").strip()[:120] or filename[:120]
        if tmpl is None:
            db.add(ImportTemplate(id=uuid.uuid4(), household_id=principal.household_id, fingerprint=cand["fingerprint"], kind=kind,
                                  mapping=fields, label=label, uses=1))
        else:
            tmpl.kind, tmpl.mapping, tmpl.label, tmpl.uses = kind, fields, label, tmpl.uses + 1
    elif tmpl is not None:
        tmpl.uses += 1
    await db.commit()
    return kind, rows, issues


async def _symbols_from_isin(principal: Principal, rows: list[Any]) -> None:
    """Brokers name shares differently (ICICI "RELIND", Kotak "RELIANCE INDUSTRIES LTD", Upstox a scrip code); the ISIN
    they all print gives the exchange symbol."""
    need = [r for r in rows if getattr(r, "isin", None) and getattr(r, "asset_type", "") in ("stock", "etf")]
    if not need:
        return
    try:
        found = await http.post(settings.market_url, "/api/v1/market/instruments/by-isin", token=principal.token,
                                json={"isins": sorted({r.isin for r in need})})
    except Exception as exc:
        log.warning("import.isin_lookup_failed", error=str(exc)[:200])
        return
    for r in need:
        hit = found.get(r.isin)
        if not hit:
            continue
        if isinstance(r, HoldingRow):
            r.symbol = hit["symbol"]
        else:
            r.name = r.name or hit.get("name")
            r.symbol, r.exchange = hit["symbol"], hit.get("exchange") or r.exchange


async def preview(db: AsyncSession, principal: Principal, filename: str, content: bytes, password: str | None, kind: str | None = None,
                  profile_hint: str | None = None, mapping: dict[str, Any] | None = None) -> dict[str, Any]:
    if len(content) > MAX_BYTES:
        raise HTTPException(413, "File too large (15 MB max)")
    kind = "unknown" if mapping else (kind or importers.detect_kind(filename, content))
    meta: dict[str, str] = {}
    tax_meta: dict[str, Any] = {}
    if kind == "cas_pdf":
        from . import depository

        try:
            cas_data = depository.read(content, password or "")
        except Exception as exc:
            raise HTTPException(422, f"Could not open this PDF (the password is usually your PAN in capitals): {exc}") from exc
        if depository.is_depository(cas_data):  # NSDL / CDSL eCAS: a holdings check across every broker, nothing imported
            report = await depository.check(db, principal, cas_data)
            return {"token": None, "kind": "demat_cas", "kind_label": KIND_LABEL["demat_cas"], "filename": filename, "expires_in": 0,
                    "rows": sum(len(a["lines"]) for a in report["accounts"]), "warnings": [], "errors": [], "error_count": 0,
                    "groups": [], "depository": report}
    try:
        if kind == "unknown":
            kind, rows, issues = await _mapped(db, principal, filename, content, mapping)
        elif kind == "cas_pdf":
            from .importers.cas import parse_cas_data

            rows, issues = parse_cas_data(cas_data)
        elif kind == "holdings":
            rows, issues = hparser.parse(content)
            meta = hparser.statement_meta(content)
        elif kind == "tax_pnl":
            from .importers import taxpnl

            rows, issues, tax_meta = taxpnl.parse(content)
            meta = {k: v for k, v in tax_meta.items() if k in ("pan", "client_id", "name")}
        elif kind == "nps_statement":
            from .importers import nps

            rows, issues = nps.parse(content)
            pran = nps.statement_meta(content).get("pran")
            meta = {"nps_pran": pran} if pran else {}
        else:
            rows, issues = importers.parse(kind, content, password)
    except HTTPException:
        raise
    except Exception as exc:  # a malformed file is the user's to fix, not a server error
        raise HTTPException(422, f"Could not read this file as {KIND_LABEL.get(kind, kind)}: {exc}") from exc
    if kind in ("holdings", "trades", "generic_csv", "zerodha_tradebook"):
        await _symbols_from_isin(principal, rows)
    if not rows and any(i.get("level") != "warning" for i in issues):
        raise HTTPException(422, "; ".join(str(i["error"]) for i in issues[:3]))

    # ---- group rows by owner
    groups: list[dict[str, Any]] = []
    if kind in ("cas_pdf", "cas_json"):
        by_pan: dict[str, list[int]] = {}
        names: dict[str, str] = {}
        for i, r in enumerate(rows):
            k = r.pan or ""
            by_pan.setdefault(k, []).append(i)
            names.setdefault(k, r.investor_name or "")
        single = len(by_pan) == 1
        for pan, idx in by_pan.items():
            # A CAS names one investor (the statement's owner) but its folios can carry other PANs
            # (family members sharing an email). Only a single-PAN statement tells us whose name it is.
            groups.append({"key": f"pan:{pan_fingerprint(pan)[:16]}" if pan else "nopan", "type": "pan" if pan else "file",
                           "pan_enc": encrypt(pan) if pan else None, "pan_masked": mask_pan(pan) if pan else None,
                           "investor": (names.get(pan) or None) if single else None, "rows": idx})
    elif kind == "tax_pnl":
        pan, cid = meta.get("pan"), meta.get("client_id")
        broker = str(tax_meta.get("broker") or "broker")
        acct = {"account_kind": broker.lower().split()[0], "account_fp": account_fingerprint(broker.lower().split()[0], cid),
                "account_label": f"{broker} {mask_tail(cid, 3)}"} if cid else {}
        if pan:
            groups.append({"key": f"pan:{pan_fingerprint(pan)[:16]}", "type": "pan", "pan_enc": encrypt(pan), "pan_masked": mask_pan(pan),
                           "investor": meta.get("name") or None, "rows": list(range(len(rows))), **acct})
        elif cid:
            groups.append({"key": f"acct:{acct['account_kind']}", "type": "account", "rows": list(range(len(rows))), **acct})
        else:
            groups.append({"key": "file", "type": "file", "rows": list(range(len(rows)))})
    elif meta.get("nps_pran"):
        pran = meta["nps_pran"]
        groups.append({"key": "acct:nps", "type": "account", "account_kind": "nps", "account_fp": account_fingerprint("nps", pran),
                       "account_label": f"NPS PRAN {mask_tail(pran, 3)}", "rows": list(range(len(rows)))})
    elif meta.get("client_id"):
        cid = meta["client_id"]
        groups.append({"key": "acct:zerodha", "type": "account", "account_kind": "zerodha", "account_fp": account_fingerprint("zerodha", cid),
                       "account_label": f"Zerodha {mask_tail(cid, 3)}", "rows": list(range(len(rows)))})
    else:
        groups.append({"key": "file", "type": "file", "rows": list(range(len(rows)))})

    # ---- match each group to a profile
    visible = set(await access.visible_profile_ids(db, principal))
    me = await _self_profile(db, principal)
    hint = None
    if profile_hint:
        hint = await db.get(Profile, uuid.UUID(profile_hint))
        if hint is None or hint.id not in visible or hint.deleted_at is not None:
            hint = None
    for g in groups:
        match: Profile | None = None
        if g["type"] == "pan":
            match = await db.scalar(select(Profile).where(Profile.household_id == principal.household_id, Profile.deleted_at.is_(None),
                                                          Profile.pan_hash == pan_fingerprint(decrypt(g["pan_enc"]))))
            g["match_reason"] = "Matched by PAN" if match else None
            if match is None and g.get("account_fp"):  # e.g. a tax statement whose Zerodha ID is already linked
                acct = await db.scalar(select(ProfileAccount).where(ProfileAccount.household_id == principal.household_id,
                                                                    ProfileAccount.kind == g["account_kind"], ProfileAccount.fingerprint == g["account_fp"]))
                match = await db.get(Profile, acct.profile_id) if acct else None
                g["match_reason"] = f"Matched: {g['account_label']} is linked" if match else None
        elif g["type"] == "account":
            acct = await db.scalar(select(ProfileAccount).where(ProfileAccount.household_id == principal.household_id,
                                                                ProfileAccount.kind == g["account_kind"], ProfileAccount.fingerprint == g["account_fp"]))
            match = await db.get(Profile, acct.profile_id) if acct else None
            g["match_reason"] = f"Matched: {g['account_label']} is linked" if match else None
        if match is not None and match.id not in visible:
            match = None
        g["matched_profile"] = {"id": str(match.id), "name": match.display_name} if match else None
        # you picked a person up front, but the statement's PAN/account belongs to someone else
        g["hint_conflict"] = match.display_name if match and hint and match.id != hint.id else None
        # what the review screen pre-selects: the match, else a new profile for an unknown PAN, else you
        if match:
            g["suggested"] = {"profile_id": str(match.id)}
        elif hint is not None and not (g["type"] == "pan" and hint.pan_hash):
            g["suggested"] = {"profile_id": str(hint.id)}
        elif g["type"] == "pan":
            g["suggested"] = {"create": g["investor"].title() if g.get("investor") else ""}
        else:
            g["suggested"] = {"profile_id": str(me.id)} if me else {}
        g["summary"] = _group_summary(kind, [rows[i] for i in g["rows"]])

    token = secrets.token_urlsafe(16)
    cls = "holdings" if kind in ("holdings", "nps_statement") else "tax" if kind == "tax_pnl" else "txn"
    statement = {k: v for k, v in tax_meta.items() if k in ("broker", "period", "checks", "charges", "notes")} if tax_meta else None
    await get_redis().set(_key(token), orjson.dumps({
        "household_id": str(principal.household_id), "user_id": str(principal.user_id), "kind": kind, "cls": cls, "filename": filename,
        "rows": _dump_rows(rows), "issues": issues, "groups": groups, "statement": statement,
    }), ex=TTL_SECONDS)
    warnings = [i for i in issues if i.get("level") == "warning"]
    errors = [i for i in issues if i.get("level") != "warning"]
    return {
        "token": token, "kind": kind, "kind_label": KIND_LABEL.get(kind, kind), "filename": filename, "expires_in": TTL_SECONDS,
        "rows": len(rows), "warnings": warnings, "errors": errors[:50], "error_count": len(errors),
        "groups": [{k: v for k, v in g.items() if k not in ("rows", "pan_enc", "account_fp")} for g in groups],
        **({"statement": statement} if statement else {}),
    }


# ------------------------------------------------------------------ commit
async def _resolve_owner(db: AsyncSession, principal: Principal, g: dict[str, Any], choice: dict[str, Any]) -> Profile:
    if choice.get("create"):
        name = str(choice["create"]).strip()[:120] or "New member"
        pan = decrypt(g["pan_enc"]) if g.get("pan_enc") else None
        return await service.new_profile(db, principal, {"display_name": name, "relationship": Relationship(choice.get("relationship") or "other")}, pan)
    if not choice.get("profile_id"):
        raise HTTPException(422, f"Choose who '{g.get('investor') or g.get('account_label') or 'this file'}' belongs to")
    prof = await access.get_profile(db, principal, uuid.UUID(str(choice["profile_id"])), write=True)
    if g.get("pan_enc"):
        pan = decrypt(g["pan_enc"])
        fp = pan_fingerprint(pan)
        if prof.pan_hash and prof.pan_hash != fp:
            who = f" ({g['investor']})" if g.get("investor") else ""
            raise HTTPException(409, f"This statement belongs to PAN {mask_pan(pan)}{who}; "
                                     f"{prof.display_name}'s PAN is XXXXXX{prof.pan_last4}. Pick the right person or create a new profile.")
        if not prof.pan_hash:
            await service.set_pan(db, prof, pan)  # tag it: the next statement matches automatically
    if g.get("account_fp"):
        linked = await db.scalar(select(ProfileAccount).where(ProfileAccount.household_id == principal.household_id,
                                                              ProfileAccount.kind == g["account_kind"], ProfileAccount.fingerprint == g["account_fp"]))
        if linked and linked.profile_id != prof.id:
            other = await db.get(Profile, linked.profile_id)
            raise HTTPException(409, f"{g['account_label']} is linked to {other.display_name if other else 'another profile'}. "
                                     "Unlink it there (Family → profile) or import into that profile.")
        if not linked:
            db.add(ProfileAccount(id=uuid.uuid4(), household_id=principal.household_id, profile_id=prof.id, kind=g["account_kind"],
                                  fingerprint=g["account_fp"], label=g["account_label"]))
    return prof


async def portfolio_of_kind(db: AsyncSession, principal: Principal, profile_id: uuid.UUID, kind: PortfolioKind, preferred: Portfolio | None = None) -> Portfolio:
    if preferred is not None and preferred.kind == kind and preferred.profile_id == profile_id:
        return preferred
    found = await db.scalar(select(Portfolio).where(Portfolio.profile_id == profile_id, Portfolio.kind == kind, Portfolio.deleted_at.is_(None))
                            .order_by(Portfolio.created_at).limit(1))
    if found:
        return found
    pf = Portfolio(id=uuid.uuid4(), household_id=principal.household_id, profile_id=profile_id, kind=kind,
                   name="Mutual Funds" if kind == PortfolioKind.MUTUAL_FUNDS else "Stocks & ETFs")
    db.add(pf)
    await db.flush()
    return pf


async def _state(principal: Principal, token: str) -> dict[str, Any]:
    raw = await get_redis().get(_key(token))
    if not raw:
        raise HTTPException(410, "This preview has expired or was already imported — upload the file again.")
    state = orjson.loads(raw)
    if state["household_id"] != str(principal.household_id):
        raise HTTPException(404, "Preview not found")
    return state


def normalise_mode(mode: str | None) -> str:
    """sync (default): each listed holding = the file; holdings that came from an earlier statement of
    the same account and are no longer listed are removed; CAS history, manual entries and other
    brokers are untouched.  replace: everything of that kind for the profile becomes the file."""
    return "replace" if mode == "replace" else "sync"


async def snapshot_sources(db: AsyncSession, household_id: uuid.UUID, account_fp: str | None) -> set[str]:
    """``source`` values of rows written by earlier holdings statements from the same account
    (statements without an account ID match other statements without one)."""
    jobs = (await db.execute(select(ImportJob).where(ImportJob.household_id == household_id, ImportJob.kind == "holdings"))).scalars()
    return {f"import:{j.id}" for j in jobs if (j.stats or {}).get("account_fp") == account_fp}


def rows_to_replace(txns: list[Transaction], mode: str, listed: set[str], snap_sources: set[str]) -> list[Transaction]:
    if mode == "replace":
        return list(txns)
    return [t for t in txns if t.symbol in listed or t.source in snap_sources]


async def commit(db: AsyncSession, principal: Principal, token: str, choices: dict[str, dict[str, Any]], mode: str = "replace") -> dict[str, Any]:
    # one reviewed import is written exactly once — a double click or a retry can't apply it twice
    r = get_redis()
    if not await r.set(f"{_key(token)}:lock", "1", nx=True, ex=300):
        raise HTTPException(409, "This import is already being written — wait for it to finish.")
    try:
        return await _commit(db, principal, token, choices, normalise_mode(mode))
    finally:
        await r.delete(f"{_key(token)}:lock")


async def _commit(db: AsyncSession, principal: Principal, token: str, choices: dict[str, dict[str, Any]], mode: str) -> dict[str, Any]:
    state = await _state(principal, token)
    cls = HoldingRow if state["cls"] == "holdings" else TaxRow if state["cls"] == "tax" else ParsedTxn
    if state["kind"] == "nps_statement":
        mode = "sync"  # an NPS statement only ever updates that PRAN's schemes — never wipes other retirement rows
    rows = _load_rows(cls, state["rows"])
    issues: list[dict[str, Any]] = state["issues"]
    results: list[dict[str, Any]] = []
    owners: list[tuple[dict[str, Any], Profile, Portfolio | None]] = []
    for g in state["groups"]:  # decide every owner first, so a 409 changes nothing
        choice = choices.get(g["key"]) or {}
        if choice.get("skip"):
            continue
        prof = await _resolve_owner(db, principal, g, choice)
        pf = None
        if cls is ParsedTxn:
            if choice.get("portfolio_id"):
                pf = await access.get_portfolio(db, principal, uuid.UUID(str(choice["portfolio_id"])), write=True)
                if pf.profile_id != prof.id:
                    raise HTTPException(422, "That portfolio belongs to a different profile")
            else:
                pf = await portfolio_of_kind(db, principal, prof.id, PortfolioKind.MUTUAL_FUNDS if state["kind"] in ("cas_pdf", "cas_json") else PortfolioKind.BROKER)
        owners.append((g, prof, pf))
    if not owners:
        raise HTTPException(422, "Nothing selected to import")
    codes = await _fund_codes(principal, token, state) if cls is HoldingRow else {}
    await get_redis().delete(_key(token))  # consumed: from here on the import is being written
    for g, prof, pf in owners:
        group_rows = [rows[i] for i in g["rows"]]
        target = pf or await portfolio_of_kind(db, principal, prof.id, PortfolioKind.RETIREMENT if state["kind"] == "nps_statement" else PortfolioKind.BROKER)
        job = ImportJob(id=uuid.uuid4(), household_id=principal.household_id, portfolio_id=target.id, kind=state["kind"],
                        filename=state["filename"], created_by=principal.user_id)
        db.add(job)
        await db.flush()
        if cls is TaxRow:
            res = await taxstore.write(db, principal.household_id, prof.id, job, group_rows, list(issues), state.get("statement") or {}, g.get("account_fp"))
        elif cls is HoldingRow:
            res = await write_holdings(db, principal, prof.id, job, group_rows, list(issues), mode=mode, codes=codes, account_fp=g.get("account_fp"))
        else:
            res = await write_transactions(db, principal, target, job, group_rows, list(issues))
        results.append({**res, "profile": {"id": str(prof.id), "name": prof.display_name}})
    return {"kind": state["kind"], "jobs": results, "status": "done" if all(r["status"] == "done" for r in results) else "partial"}


async def _fund_codes(principal: Principal, token: str, state: dict[str, Any]) -> dict[str, Any]:
    """AMFI codes for the statement's funds (ISIN → code), resolved once and kept with the preview."""
    if "codes" in state:
        return state["codes"]
    rows = _load_rows(HoldingRow, state["rows"])
    funds = [r for r in rows if r.asset_type == "mutual_fund"]
    codes: dict[str, Any] = {}
    if funds:
        try:
            codes = await http.post(settings.market_url, "/api/v1/market/mf/resolve", token=principal.token,
                                    json={"items": [{"key": r.source_row, "isin": r.isin, "name": r.name} for r in funds]})
        except Exception as exc:
            log.warning("import.fund_resolve_failed", error=str(exc))
            return {}  # not cached: try again on the next step
    state["codes"] = codes
    await get_redis().set(_key(token), orjson.dumps(state), keepttl=True)
    return codes


def _snapshot_items(rows: list[HoldingRow], codes: dict[str, Any]) -> tuple[dict[tuple[PortfolioKind, str], HoldingRow], dict[tuple[PortfolioKind, str], str | None], list[HoldingRow]]:
    """Statement rows → one item per (portfolio kind, instrument symbol). Rows for the same
    instrument (e.g. X and X-F fractional units, a fund listed twice) are merged."""
    items: dict[tuple[PortfolioKind, str], HoldingRow] = {}
    hints: dict[tuple[PortfolioKind, str], str | None] = {}
    unmatched: list[HoldingRow] = []
    for r in rows:
        if r.asset_type == "mutual_fund":
            hit = codes.get(r.source_row)
            if not hit:
                unmatched.append(r)
                continue
            key, hint = (PortfolioKind.MUTUAL_FUNDS, str(hit["code"])), hit.get("name") or r.name
        elif r.asset_type == "nps":
            key, hint = (PortfolioKind.RETIREMENT, r.symbol or ""), r.name
        else:
            key, hint = (PortfolioKind.BROKER, f"{r.symbol}.NS" if r.symbol and "." not in r.symbol else (r.symbol or "")), None
        if key in items:
            prev = items[key]
            items[key] = dataclasses.replace(prev, quantity=prev.quantity + r.quantity, invested=prev.invested + r.invested,
                                             long_term_qty=prev.long_term_qty + r.long_term_qty,
                                             avg_price=((prev.invested + r.invested) / (prev.quantity + r.quantity)).quantize(Decimal("0.0001")))
        else:
            items[key], hints[key] = r, hint
    return items, hints, unmatched


async def _portfolios_of_kind(db: AsyncSession, profile_id: uuid.UUID, kind: PortfolioKind) -> list[Portfolio]:
    return list((await db.execute(select(Portfolio).where(Portfolio.profile_id == profile_id, Portfolio.kind == kind,
                                                          Portfolio.deleted_at.is_(None)))).scalars())


def _positions(txns: list[Transaction]) -> dict[str, dict[str, Any]]:
    """Quantity + invested per instrument symbol (all given portfolios summed)."""
    out: dict[str, dict[str, Any]] = {}
    for pos in build_positions([to_txn(t) for t in txns]).values():
        if pos.qty <= 0:
            continue
        cur = out.setdefault(pos.symbol, {"qty": Decimal(0), "invested": Decimal(0)})
        cur["qty"] += pos.qty
        cur["invested"] += pos.invested
    return out


async def plan(db: AsyncSession, principal: Principal, token: str, key: str, profile_id: str, mode: str = "replace",
               portfolio_id: str | None = None) -> dict[str, Any]:
    """Exactly what an import would change for one owner — shown before anything is written."""
    state = await _state(principal, token)
    g = next((x for x in state["groups"] if x["key"] == key), None)
    if g is None:
        raise HTTPException(404, "Unknown group")
    prof = await access.get_profile(db, principal, uuid.UUID(profile_id))
    mode = "sync" if state["kind"] == "nps_statement" else normalise_mode(mode)
    if state["cls"] == "tax":
        rows = [r for i, r in enumerate(_load_rows(TaxRow, state["rows"])) if i in set(g["rows"])]
        return {**await taxstore.plan(db, prof.id, rows, state.get("statement") or {}, g.get("account_fp")), "profile": prof.display_name}
    if state["cls"] == "holdings":
        rows = [r for i, r in enumerate(_load_rows(HoldingRow, state["rows"])) if i in set(g["rows"])]
        codes = await _fund_codes(principal, token, state)
        items, hints, unmatched = _snapshot_items(rows, codes)
        kinds = sorted({k for k, _ in items}, key=lambda k: k.value)
        pfs = {k: await _portfolios_of_kind(db, prof.id, k) for k in kinds}
        snap = await snapshot_sources(db, principal.household_id, g.get("account_fp"))
        current: dict[PortfolioKind, dict[str, dict[str, Any]]] = {}
        after_unlisted: dict[PortfolioKind, dict[str, dict[str, Any]]] = {}
        for k, v in pfs.items():
            txns = await load_txns(db, principal.household_id, [p.id for p in v])
            gone = {t.id for t in rows_to_replace(txns, mode, {sym for kk, sym in items if kk == k}, snap)}
            current[k] = _positions(txns)
            after_unlisted[k] = _positions([t for t in txns if t.id not in gone])
        out: list[dict[str, Any]] = []
        for (k, sym), r in sorted(items.items(), key=lambda kv: kv[1].name):
            now = current[k].get(sym, {"qty": Decimal(0), "invested": Decimal(0)})
            status = "new" if now["qty"] == 0 else "same" if abs(now["qty"] - r.quantity) < Decimal("0.0005") else "update"
            out.append({"name": hints.get((k, sym)) or r.name, "symbol": sym, "asset": "fund" if k == PortfolioKind.MUTUAL_FUNDS else r.asset_type,
                        "now": float(now["qty"]), "file": float(r.quantity), "after": float(r.quantity), "status": status,
                        "invested_now": float(now["invested"]), "invested_after": float(r.invested)})
        listed = set(items)
        for k in kinds:
            for sym, now in current[k].items():
                if (k, sym) in listed:
                    continue
                aft = after_unlisted[k].get(sym, {"qty": Decimal(0), "invested": Decimal(0)})
                changed = abs(aft["qty"] - now["qty"]) >= Decimal("0.0005")
                out.append({"name": sym, "symbol": sym, "asset": "fund" if k == PortfolioKind.MUTUAL_FUNDS else "stock",
                            "now": float(now["qty"]), "file": 0.0, "after": float(aft["qty"]),
                            "status": ("remove" if aft["qty"] <= 0 else "update") if changed else "kept",
                            "invested_now": float(now["invested"]), "invested_after": float(aft["invested"])})
        for r in unmatched:
            out.append({"name": r.name, "symbol": r.isin or "", "asset": "fund", "now": None, "file": float(r.quantity), "after": None,
                        "status": "unmatched", "invested_now": None, "invested_after": None})
        counts: dict[str, int] = {}
        for x in out:
            counts[x["status"]] = counts.get(x["status"], 0) + 1
        return {"kind": "holdings", "mode": mode, "profile": prof.display_name, "rows": out, "counts": counts,
                "portfolios": [p.name for v in pfs.values() for p in v] or ["(new) " + ("Mutual Funds" if k == PortfolioKind.MUTUAL_FUNDS else "Stocks & ETFs") for k in kinds],
                "invested_now": float(sum((c["invested"] for cur in current.values() for c in cur.values()), Decimal(0))),
                "invested_after": float(sum((x["invested_after"] or 0) for x in out))}
    # transaction files: which rows are new vs already imported in the target portfolio
    rows = [r for i, r in enumerate(_load_rows(ParsedTxn, state["rows"])) if i in set(g["rows"])]
    if portfolio_id:
        pf = await access.get_portfolio(db, principal, uuid.UUID(portfolio_id))
    else:
        kind = PortfolioKind.MUTUAL_FUNDS if state["kind"] in ("cas_pdf", "cas_json") else PortfolioKind.BROKER
        pf = next(iter(await _portfolios_of_kind(db, prof.id, kind)), None)
    existing: set[str] = set()
    if pf is not None:
        existing = set((await db.execute(select(Transaction.client_ref).where(Transaction.portfolio_id == pf.id, Transaction.deleted_at.is_(None),
                                                                            Transaction.client_ref.is_not(None)))).scalars())
    new = dup = 0
    occ: dict[str, int] = {}
    for r in sorted(rows, key=lambda r: r.trade_date):
        k_ = occ.get(r.fingerprint, 0)
        occ[r.fingerprint] = k_ + 1
        ref = r.fingerprint if k_ == 0 else f"{r.fingerprint}#{k_}"
        if ref in existing:
            dup += 1
        else:
            new += 1
    return {"kind": "txn", "profile": prof.display_name, "portfolio": pf.name if pf else "(new portfolio)", "counts": {"new": new, "already_imported": dup}}


# ------------------------------------------------------------------ writers
async def _rollback(sp: Any) -> None:
    if sp is not None and sp.is_active:
        await sp.rollback()


async def write_transactions(db: AsyncSession, principal: Principal, pf: Portfolio, job: ImportJob, parsed: list[ParsedTxn],
                             issues: list[dict[str, Any]]) -> dict[str, Any]:
    warnings = [e for e in issues if e.get("level") == "warning"]
    errors = [e for e in issues if e.get("level") != "warning"]
    created = skipped = 0
    resolved: dict[str, dict[str, Any]] = {}
    occurrences: dict[str, int] = {}
    for row in sorted(parsed, key=lambda r: r.trade_date):
        label = f"{row.name or row.symbol} · {row.trade_date}"
        # identical rows in one file are real (e.g. two equal SIP instalments on one day):
        # number them so both are kept and a re-import still recognises each one
        k = occurrences.get(row.fingerprint, 0)
        occurrences[row.fingerprint] = k + 1
        ref = row.fingerprint if k == 0 else f"{row.fingerprint}#{k}"
        sp = None
        try:
            key = row.yahoo_symbol
            if key not in resolved:
                resolved[key] = await service.resolve_instrument(principal, None, key, row.asset_type, row.name, row.isin)
            sp = await db.begin_nested()  # a bad row must not poison the whole import
            _, was_created = await service.record(
                db, principal, pf, resolved[key], txn_type=TxnType(row.txn_type), trade_date=row.trade_date,
                quantity=row.quantity, price=row.price, fees=row.fees, amount=row.amount or None,
                client_ref=ref, source=f"import:{job.id}", validate=False,
                notes="Estimated from statement (opening balance / summary)" if row.estimated else "",
            )
            await db.flush()
            await sp.commit()
            created += was_created
            skipped += not was_created
        except HTTPException as exc:
            await _rollback(sp)
            errors.append({"row": label, "error": str(exc.detail)})
        except Exception as exc:
            await _rollback(sp)
            log.exception("import.row_failed", row=label)
            errors.append({"row": label, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
    await db.flush()
    try:
        await service.validate_ledger(db, principal.household_id, pf)
    except HTTPException as exc:
        errors.append({"row": "ledger", "error": f"Imported ledger has oversold positions: {exc.detail}. Import earlier history first."})
    job.status = "done" if created or not errors else "failed"
    job.stats = {"parsed": len(parsed), "created": created, "duplicates_skipped": skipped, "errors": len(errors),
                 "warnings": len(warnings), "instruments": len(resolved), "portfolios": [pf.name]}
    job.errors = (warnings + errors)[:200]
    await db.commit()
    if created:
        await publish("import.completed", {"job_id": str(job.id), "portfolio_id": str(pf.id), "profile_id": str(pf.profile_id),
                                           **{k: v for k, v in job.stats.items() if k != "portfolios"}}, household_id=str(principal.household_id))
    return {"id": str(job.id), "kind": job.kind, "status": job.status, "stats": job.stats, "errors": job.errors}


async def write_holdings(db: AsyncSession, principal: Principal, profile_id: uuid.UUID, job: ImportJob, rows: list[HoldingRow],
                         issues: list[dict[str, Any]], *, mode: str = "sync", codes: dict[str, Any] | None = None,
                         account_fp: str | None = None) -> dict[str, Any]:
    """A holdings statement is a snapshot of *now*: each instrument becomes one estimated position at
    its average cost (stocks → the profile's broker portfolio, funds → its Mutual Funds portfolio).

    Neither mode can double a holding (see ``normalise_mode``). Replaced rows are tombstoned — never
    deleted — and their ids are kept on the job, so deleting this import restores them.
    """
    errors = [e for e in issues if e.get("level") != "warning"]
    warnings: list[dict[str, Any]] = [e for e in issues if e.get("level") == "warning"]
    items, hints, unmatched = _snapshot_items(rows, codes or {})
    for r in unmatched:
        errors.append({"row": r.source_row, "error": f"Couldn't match this fund{f' (ISIN {r.isin})' if r.isin else ''} to an AMFI scheme code — "
                       "the AMFI list may be unreachable (see Settings → Data sources); left unchanged. Re-import later."})
    today = date.today()
    kinds = {k for k, _ in items}
    targets = {k: await portfolio_of_kind(db, principal, profile_id, k) for k in sorted(kinds, key=lambda k: k.value)}
    all_pfs = {k: await _portfolios_of_kind(db, profile_id, k) for k in kinds}

    replaced_ids: list[str] = []
    if items:
        now_ts = datetime.now().astimezone()
        snap = await snapshot_sources(db, principal.household_id, account_fp)
        for k in kinds:
            pf_ids = [p.id for p in all_pfs[k]] or [targets[k].id]
            live = await load_txns(db, principal.household_id, pf_ids)
            gone = rows_to_replace(live, mode, {sym for kk, sym in items if kk == k}, snap)
            for t in gone:
                # client_ref is kept: a restore brings the row back intact, and a re-import still
                # recognises it (service.record frees a tombstoned row's ref before inserting)
                t.deleted_at, t.version = now_ts, t.version + 1
                replaced_ids.append(str(t.id))
            touched = {t.source for t in gone if t.source and t.source.startswith("import:")}
            await db.flush()  # the session doesn't autoflush — the 'still live?' check below must see the tombstones
            for old in (await db.execute(select(ImportJob).where(ImportJob.portfolio_id.in_(pf_ids), ImportJob.id != job.id,
                                                                 ImportJob.status.not_in(["deleted", "replaced"])))).scalars():
                if f"import:{old.id}" in touched and not (await db.scalar(select(Transaction.id).where(
                        Transaction.source == f"import:{old.id}", Transaction.deleted_at.is_(None)).limit(1))):
                    old.status = "replaced"
        await db.flush()
    replaced = len(replaced_ids)
    planned = [(targets[k], r, sym, hints.get((k, sym))) for (k, sym), r in items.items()]

    created = 0
    resolved: dict[str, dict[str, Any]] = {}
    ref_prices: dict[str, dict[str, Any]] = {}
    for n, (tpf, r, sym, hint) in enumerate(planned):
        sp = None
        try:
            if sym not in resolved:
                resolved[sym] = await service.resolve_instrument(principal, None, sym, r.asset_type, hint or r.name, r.isin, r.sector)
            inst = resolved[sym]
            sp = await db.begin_nested()  # a bad row must not poison the whole import
            # Zerodha tells us how many units are long-term (held > 1 year): date those a year+ back
            # so tax-timing advice isn't told everything is short-term.
            lots = [(r.long_term_qty, today - timedelta(days=400)), (r.quantity - r.long_term_qty, today)] if r.long_term_qty > 0 else [(r.quantity, today)]
            for i, (q, when) in enumerate(lots):
                if q <= 0:
                    continue
                _, was = await service.record(
                    db, principal, tpf, inst, txn_type=TxnType.BUY, trade_date=when, quantity=q, price=r.avg_price,
                    amount=(r.invested * q / r.quantity).quantize(Decimal("0.01")) if r.quantity else None,
                    client_ref=f"hold:{job.id.hex[:12]}:{n}:{i}", source=f"import:{job.id}", validate=False,
                    notes="From holdings statement — average cost; purchase date estimated",
                )
                created += was
            await db.flush()
            await sp.commit()
            if r.price:
                ref_prices[inst["symbol"]] = {"price": float(r.price), "as_of": (getattr(r, "price_date", None) or today).isoformat()}
        except HTTPException as exc:
            await _rollback(sp)
            errors.append({"row": r.source_row, "error": str(exc.detail)})
        except Exception as exc:
            await _rollback(sp)
            log.exception("import.holdings_row_failed", row=r.source_row)
            errors.append({"row": r.source_row, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
    if ref_prices:
        try:
            await http.post(settings.market_url, "/api/v1/market/quotes/reference", token=principal.token, json={"prices": ref_prices})
        except Exception as exc:
            log.warning("import.reference_prices_failed", error=str(exc))
    if replaced:
        warnings.append({"row": "statement", "level": "warning",
                         "error": (f"Replaced {replaced} earlier transaction(s) in {', '.join(p.name for v in all_pfs.values() for p in v)} with this snapshot."
                                   if mode == "replace" else f"Synced {len(items)} holding(s) to the statement ({replaced} earlier row(s) replaced). "
                                   "Delete this import to undo — the replaced rows come back.")})
    warnings.append({"row": "statement", "level": "warning",
                     "error": "Holdings statements carry average cost but not purchase dates — returns are shown as absolute and tax lots are estimated."})
    job.status = "done" if created or not errors else "failed"
    job.stats = {"parsed": len(rows), "created": created, "duplicates_skipped": 0, "errors": len(errors), "warnings": len(warnings),
                 "instruments": len(resolved), "replaced": replaced, "mode": mode, "portfolios": [t.name for t in targets.values()],
                 "account_fp": account_fp, "replaced_ids": replaced_ids}
    job.errors = (warnings + errors)[:200]
    await db.commit()
    if created:
        for tpf in targets.values():
            await publish("import.completed", {"job_id": str(job.id), "portfolio_id": str(tpf.id), "profile_id": str(tpf.profile_id),
                                               **{k: v for k, v in job.stats.items() if k != "portfolios"}}, household_id=str(principal.household_id))
    return {"id": str(job.id), "kind": "holdings", "status": job.status, "stats": job.stats, "errors": job.errors}
