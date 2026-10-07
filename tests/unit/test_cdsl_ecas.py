"""CDSL eCAS (demat + mutual-fund statement) read from its tables — a synthetic PDF in CDSL's layout, made-up data."""
from decimal import Decimal

import pytest

fitz = pytest.importorskip("fitz")

from portfolio_svc.depository import _with_fund_units, is_depository, read  # noqa: E402
from portfolio_svc.importers import ecas  # noqa: E402


def _table(page, x, y, widths, rows, h=22):
    for r, row in enumerate(rows):
        rh = h * 2 if r == 0 else h  # headings wrap onto two lines, as in the real statement
        cx = x
        for w, cell in zip(widths, row, strict=True):
            rect = fitz.Rect(cx, y, cx + w, y + rh)
            page.draw_rect(rect, color=(0, 0, 0), width=0.6)
            page.insert_textbox(rect + (2, 2, -2, -2), str(cell), fontsize=6)
            cx += w
        y += rh
    return y


def cdsl_pdf(password: str = "") -> bytes:
    doc = fitz.open()
    p = doc.new_page(width=842, height=595)
    p.insert_text((40, 30), "CONSOLIDATED ACCOUNT STATEMENT (CAS) FOR SECURITIES HELD IN DEMAT FORM AND INVESTMENTS IN MUTUAL FUNDS", fontsize=7)
    p.insert_text((40, 42), "Central Depository Services (India) Limited   Statement for the period from 01-08-2026 to 31-08-2026", fontsize=7)
    p.insert_text((40, 54), "PAN: ABCDE1234F   A SAMPLE INVESTOR", fontsize=7)
    p.insert_text((40, 72), "DP Name : SAMPLE BROKING LIMITED   BO ID : 1208160000123456", fontsize=7)
    y = _table(p, 40, 80, [70, 160, 140, 50, 50, 50, 50, 50, 40], [
        ["ISIN", "Security", "Transaction Particulars", "Date", "Op. Bal", "Credit", "Debit", "Cl. Bal", "Stamp Duty"],
        ["INE090A01021", "ICICI BANK LIMITED", "OF-CR TD:1 TX:2", "13-08-2026", "65.000", "103.000", "--", "168.000", "0"]])
    p.insert_text((40, y + 14), "HOLDING STATEMENT AS ON 31-08-2026", fontsize=7)
    y = _table(p, 40, y + 20, [70, 170, 60, 60, 60, 70, 80], [
        ["ISIN", "Security", "Current Bal", "Frozen Bal", "Free Bal", "Market Price / Face Value", "Value (₹)"],
        ["INE090A01021", "ICICI BANK LIMITED", "168.000", "0.000", "168.000", "1,250.50", "2,10,084.00"],
        ["INE002A01018", "RELIANCE INDUSTRIES LTD", "10.000", "0.000", "10.000", "2,900.00", "29,000.00"]])
    p.insert_text((40, y + 14), "HOLDING STATEMENT AS ON 31-08-2026 (Other Details)", fontsize=7)
    y = _table(p, 40, y + 20, [70, 200, 60, 80, 60, 60, 60], [
        ["ISIN", "Security", "Date", "Balance Description", "Lockin", "Pending Demat", "Pending Remat"],
        ["INF194K01Y29", "BANDHAN ELSS TAX SAVER FUND - DIRECT PL - GROWTH", "03-10-2028", "ELSS2005", "51.448", "0.000", "0.000"]])
    p2 = doc.new_page(width=842, height=595)
    p2.insert_text((40, 40), "MUTUAL FUND UNITS HELD AS ON 31-08-2026", fontsize=7)
    _table(p2, 40, 50, [170, 70, 70, 60, 60, 80, 70, 70, 50], [
        ["Scheme Name", "ISIN", "Folio No.", "Closing Bal (Units)", "NAV (₹)", "Cumulative Amount Invested (in INR)",
         "Valuation (₹)", "Unrealised Profit/Loss", "Unrealised Profit/Loss(%)"],
        ["32Z - Aditya Birla Sun Life Corporate Bond Fund - Growth-Direct Plan", "INF209K01S38", "1040000001", "11.63", "121.6418",
         "1,000.00", "1,414.69", "414.69", "41.47"]])
    opts = {"encryption": fitz.PDF_ENCRYPT_AES_256, "user_pw": password, "owner_pw": password + "o"} if password else {}
    return doc.tobytes(**opts)


def test_cdsl_tables_are_recognised_by_their_headings():
    assert ecas.classify(["ISIN", "Security", "Transaction Particulars", "Date", "Op. Bal", "Credit", "Debit", "Cl. Bal"]) == "transactions"
    assert ecas.classify(["ISIN", "Security", "Current Bal", "Free Bal", "Market Price", "Value"]) == "holdings"
    assert ecas.classify(["ISIN", "Security", "Date", "Balance Description", "Lockin"]) == "lockin"
    assert ecas.classify(["Scheme Name", "ISIN", "Folio No.", "Closing Bal (Units)", "NAV"]) == "funds"


def test_a_cdsl_statement_becomes_accounts_and_fund_units():
    data = read(cdsl_pdf("ABCDE1234F"), "ABCDE1234F")
    assert data["file_type"] == "CDSL" and is_depository(data)
    assert data["statement_period"] == {"from": "01-08-2026", "to": "31-08-2026"}
    [acct] = data["accounts"]
    assert acct["client_id"] == "1208160000123456" and acct["owners"][0]["pan"] == "ABCDE1234F"
    assert acct["name"].startswith("SAMPLE BROKING")
    assert [(e["isin"], Decimal(e["num_shares"]), Decimal(e["value"])) for e in acct["equities"]] == [
        ("INE090A01021", Decimal("168.000"), Decimal("210084.00")), ("INE002A01018", Decimal("10.000"), Decimal("29000.00"))]
    [mf] = data["mutual_funds"]  # the lock-in table is not a holding
    assert mf["isin"] == "INF209K01S38" and Decimal(mf["units"]) == Decimal("11.63") and Decimal(mf["invested"]) == 1000
    checked = _with_fund_units(data)["accounts"]
    assert checked[-1]["type"] == "MF" and checked[-1]["equities"][0]["isin"] == "INF209K01S38"


def test_wrong_password_is_reported_as_such():
    with pytest.raises(ValueError, match="password"):
        read(cdsl_pdf("ABCDE1234F"), "WRONG")
