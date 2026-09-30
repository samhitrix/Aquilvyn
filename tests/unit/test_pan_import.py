"""PAN fingerprints, statement account IDs and the preview row round-trip."""
import io
from datetime import date
from decimal import Decimal

import pytest

from fm_common.crypto import account_fingerprint, mask_pan, pan_fingerprint


def test_pan_fingerprint_is_case_and_space_insensitive_and_not_the_pan():
    fp = pan_fingerprint("abcde1234f")
    assert fp == pan_fingerprint(" ABCDE 1234F ") and len(fp) == 64 and "ABCDE" not in fp
    assert fp != pan_fingerprint("ABCDE1234G")
    assert account_fingerprint("zerodha", "ab1234") == account_fingerprint("Zerodha", "AB1234") != fp


def test_mask_pan_matches_api_format():
    assert mask_pan("abcde1234f") == "XXXXXX234F"


def test_zerodha_client_id_is_read_from_the_statement():
    openpyxl = pytest.importorskip("openpyxl")
    from portfolio_svc.importers.holdings import statement_meta

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([None, "Client ID", "ab1234"])
    ws.append([])
    ws.append(["Symbol", "ISIN", "Quantity Available", "Average Price"])
    buf = io.BytesIO()
    wb.save(buf)
    assert statement_meta(buf.getvalue()) == {"client_id": "AB1234"}
    assert statement_meta(b"Instrument,Qty.,Avg. cost\nTCS,1,3000\n") == {}


def test_preview_rows_round_trip_through_json():
    import orjson

    from portfolio_svc.importers.base import ParsedTxn
    from portfolio_svc.importers.holdings import HoldingRow
    from portfolio_svc.imports import _dump_rows, _load_rows

    t = ParsedTxn(symbol="120716", asset_type="mutual_fund", txn_type="buy", trade_date=date(2024, 1, 5), quantity=Decimal("10.5"),
                  price=Decimal("100"), amount=Decimal("1050"), pan="ABCDE1234F", estimated=True)
    h = HoldingRow(name="TCS", symbol="TCS", isin=None, asset_type="stock", quantity=Decimal("3"), avg_price=Decimal("3400"),
                   invested=Decimal("10200"), price=None)
    for cls, row in ((ParsedTxn, t), (HoldingRow, h)):
        back = _load_rows(cls, orjson.loads(orjson.dumps(_dump_rows([row]))))[0]
        assert back == row
    assert _load_rows(ParsedTxn, orjson.loads(orjson.dumps(_dump_rows([t]))))[0].fingerprint == t.fingerprint
