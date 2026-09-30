"""Corporate Action Engine (E35) — ledger side. Applies splits/bonuses (and cash dividends) from
market-svc to every portfolio holding the instrument on the ex-date, exactly once."""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from fm_common.db.session import SessionLocal
from fm_common.logging import get_logger

from .holdings import to_txn
from .ledger import build_positions
from .models import AppliedCorporateAction, Portfolio, Transaction, TxnType

log = get_logger(__name__)


async def apply(action: dict[str, Any]) -> int:
    iid = uuid.UUID(action["instrument_id"])
    ex = date.fromisoformat(action["ex_date"])
    kind = action["action_type"]
    if kind not in ("split", "bonus", "dividend"):
        return 0
    ca_id = action.get("id") or f"{iid}:{kind}:{ex}"
    ca_uuid = uuid.uuid5(uuid.NAMESPACE_URL, str(ca_id))
    applied = 0
    async with SessionLocal() as db:
        pf_ids = list((await db.execute(select(Transaction.portfolio_id).where(Transaction.instrument_id == iid, Transaction.deleted_at.is_(None)).distinct())).scalars())
        for pf_id in pf_ids:
            done = await db.scalar(select(AppliedCorporateAction.portfolio_id).where(AppliedCorporateAction.portfolio_id == pf_id, AppliedCorporateAction.corporate_action_id == ca_uuid))
            if done:
                continue
            txns = (await db.execute(select(Transaction).where(Transaction.portfolio_id == pf_id, Transaction.instrument_id == iid,
                                                              Transaction.deleted_at.is_(None), Transaction.trade_date < ex))).scalars().all()
            if not txns:
                continue
            pos = build_positions([to_txn(t) for t in txns]).get(str(iid))
            held = pos.qty if pos else Decimal(0)
            if held <= 0:
                continue
            pf = await db.get(Portfolio, pf_id)
            t0 = txns[0]
            rf, rt = Decimal(str(action.get("ratio_from") or 1)), Decimal(str(action.get("ratio_to") or 1))
            if kind == "split":
                ttype, qty, amount = TxnType.SPLIT, rt / rf, Decimal(0)
            elif kind == "bonus":
                ttype, qty, amount = TxnType.BONUS, (held * rt / rf).quantize(Decimal("1.")), Decimal(0)
            else:
                ttype, qty, amount = TxnType.DIVIDEND, Decimal(0), (held * Decimal(str(action.get("amount") or 0))).quantize(Decimal("0.01"))
                if amount <= 0:
                    continue
            t = Transaction(
                id=uuid.uuid4(), household_id=pf.household_id, portfolio_id=pf_id, instrument_id=iid, symbol=t0.symbol,
                asset_type=t0.asset_type, txn_type=ttype, trade_date=ex if kind != "dividend" else ex + timedelta(days=0),
                quantity=qty, price=Decimal(0), amount=amount, source="corporate_action", client_ref=f"ca:{ca_uuid}",
                notes=f"Auto-applied {kind} ({action.get('ratio_from') or ''}:{action.get('ratio_to') or action.get('amount') or ''})",
            )
            db.add(t)
            await db.execute(insert(AppliedCorporateAction).values(portfolio_id=pf_id, corporate_action_id=ca_uuid, transaction_id=t.id).on_conflict_do_nothing())
            applied += 1
        await db.commit()
    if applied:
        log.info("corporate_action.applied", symbol=action.get("symbol"), kind=kind, portfolios=applied)
    return applied
