"""CAS → ledger mapping, using casparser-shaped data with its real enum types (the
'TRANSACTIONTYPE.PURCHASE' bug came from str()-ing those enums)."""
from datetime import date
from decimal import Decimal

import pytest

from portfolio_svc.importers.cas import parse_cas_data

casparser = pytest.importorskip("casparser")
from casparser.enums import CASFileType, TransactionType  # noqa: E402


def _t(d, typ, amount=None, units=None, nav=None, desc=""):
    return {"date": d, "description": desc, "amount": amount, "units": units, "nav": nav, "type": typ}


def _cas(schemes, cas_type=CASFileType.DETAILED, pan="ABCDE1234F", period_from="01-Apr-2020"):
    return {
        "cas_type": cas_type, "statement_period": {"from": period_from, "to": "31-Mar-2026"},
        "investor_info": {"name": "RAVI KUMAR", "email": "", "address": "", "mobile": ""},
        "folios": [{"folio": "123/45", "amc": "HDFC", "PAN": pan, "schemes": schemes}],
    }


def _scheme(txns, open_=0, close=0, cost=None, nav=100, amfi="118989"):
    return {"scheme": "HDFC Mid-Cap Opportunities Fund - Direct Growth", "amfi": amfi, "isin": "INF179K01XQ0", "rta": "CAMS",
            "rta_code": "H01", "open": open_, "close": close, "close_calculated": close,
            "valuation": {"date": date(2026, 3, 31), "nav": nav, "cost": cost, "value": close * nav}, "transactions": txns}


def test_enum_types_map_and_tax_rows_are_skipped():
    rows, errs = parse_cas_data(_cas([_scheme([
        _t(date(2021, 1, 5), TransactionType.PURCHASE, 10000, 100, 100),
        _t(date(2021, 1, 5), TransactionType.STAMP_DUTY_TAX, 0.5),
        _t(date(2021, 2, 5), TransactionType.PURCHASE_SIP, 5000, 45.4545, 110),
        _t(date(2021, 2, 5), TransactionType.STAMP_DUTY_TAX, 0.25),
        _t(date(2022, 3, 1), TransactionType.REDEMPTION, 3000, -20, 150),
        _t(date(2022, 3, 1), TransactionType.STT_TAX, 0.03),
        _t(date(2022, 6, 1), TransactionType.DIVIDEND_PAYOUT, 120),
    ], close=125.4545)]))
    assert not [e for e in errs if e.get("level") != "warning"], errs
    assert [r.txn_type for r in rows] == ["buy", "sip", "sell", "dividend"]
    assert rows[0].fees == Decimal("0.5") and rows[1].fees == Decimal("0.25")  # stamp duty folded into cost
    assert rows[2].quantity == Decimal("20")  # redemption units are negative in CAS
    assert rows[3].amount == Decimal("120") and rows[3].quantity == 0
    assert all(r.pan == "ABCDE1234F" and r.folio == "123/45" and r.investor_name == "RAVI KUMAR" for r in rows)
    assert len({r.fingerprint for r in rows}) == 4


def test_string_types_also_work():
    rows, errs = parse_cas_data(_cas([_scheme([_t("2021-01-05", "PURCHASE", 1000, 10, 100)], close=10)]))
    assert rows[0].txn_type == "buy" and rows[0].trade_date == date(2021, 1, 5) and not errs


def test_detailed_opening_balance_is_estimated():
    rows, errs = parse_cas_data(_cas([_scheme([_t(date(2021, 1, 5), TransactionType.PURCHASE, 10000, 100, 100)],
                                              open_=50, close=150, cost=14000)]))
    assert rows[0].estimated and rows[0].quantity == Decimal("50") and rows[0].trade_date == date(2020, 4, 1)
    assert rows[0].price == Decimal("80.0000")  # (14000 − 10000 bought in period) / 50 opening units
    assert any(e.get("level") == "warning" and "opening balance" in e["error"] for e in errs)


def test_summary_cas_imports_holdings_with_warning():
    rows, errs = parse_cas_data(_cas([_scheme([], open_=0, close=200, cost=30000, nav=180)], cas_type=CASFileType.SUMMARY))
    assert len(rows) == 1 and rows[0].estimated and rows[0].quantity == Decimal("200") and rows[0].price == Decimal("150.0000")
    assert any("Summary CAS" in e["error"] for e in errs)


def test_errors_name_the_scheme_and_reversals_warn():
    rows, errs = parse_cas_data(_cas([
        _scheme([_t(date(2021, 1, 5), TransactionType.REVERSAL, -1000, -10, 100, "Reversal of purchase")]),
        _scheme([], amfi=""),
    ]))
    assert not rows
    assert any(e.get("level") == "warning" and "Reversal" in e["error"] for e in errs)
    assert any("HDFC Mid-Cap" in str(e["row"]) and "AMFI" in e["error"] for e in errs)
