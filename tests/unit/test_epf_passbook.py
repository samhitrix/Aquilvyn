"""EPFO member passbook PDF → EPF ledger entries — a synthetic passbook in EPFO's layout, made-up data."""
from datetime import date
from decimal import Decimal

import pytest

fitz = pytest.importorskip("fitz")

from portfolio_svc.importers import detect_kind, epf  # noqa: E402

MEMBER = "ABCDE00000000000012345"


def passbook(rows=None, closing=("12,22,647", "8,18,369", "61,250")) -> bytes:
    doc = fitz.open()
    p = doc.new_page(width=842, height=595)
    y = 30

    def line(*cells, xs=(30, 110, 180, 210, 330, 400, 470, 540, 610, 680)):
        nonlocal y
        for x, c in zip(xs, cells, strict=False):
            p.insert_text((x, y), str(c), fontsize=7)
        y += 14

    line("EPF Passbook [ Financial Year - 2026-2027 ]", xs=(200,))
    line("Establishment ID/Name : ABCDE0000000000 / SAMPLE TECHNOLOGIES PVT LTD", xs=(30,))
    line(f"Member ID/Name : {MEMBER} / SAMPLE MEMBER", xs=(30,))
    line("UAN : 100000000001", xs=(30,))
    line("Particulars", "Employee Balance", "Employer Balance", "Pension Balance", xs=(30, 540, 610, 680))
    line("OB Int. Updated upto 31/03/2026", "10,95,147", "6,98,369", "53,750", xs=(30, 540, 610, 680))
    for wm, d, due in rows or [("Mar-2026", "01-04-2026", "042026"), ("Apr-2026", "01-05-2026", "052026"), ("May-2026", "01-06-2026", "062026"),
                                ("Jun-2026", "15-08-2026", "072026"), ("Jul-2026", "15-08-2026", "082026"), ("Aug-2026", "09-09-2026", "092026")]:
        line(wm, d, "CR", f"Cont. for Due-Month {due}", "1,77,080", "15,000", "21,250", "20,000", "1,250", xs=(30, 80, 130, 150, 330, 400, 470, 540, 610))
    line("Total Contributions for the year [ 2026 ]", "1,27,500", "1,20,000", "7,500", xs=(200, 540, 610, 680))
    line("Interest details N/A", "0", "0", "0", xs=(30, 540, 610, 680))
    line("Closing Balance as on 31/03/2027", *closing, xs=(30, 540, 610, 680))
    return doc.tobytes()


def test_epf_passbook_is_recognised_and_needs_no_password():
    assert detect_kind("BGBNG000_2026.pdf", passbook()) == "epf_passbook"


def test_passbook_becomes_opening_balance_contributions_and_matches_its_closing_balance():
    rows, issues, meta = epf.parse(passbook())
    assert meta["member_id"] == MEMBER and meta["fy"] == "2026-2027" and meta["name"] == "Sample Member"
    assert meta["establishment"].startswith("SAMPLE TECHNOLOGIES")
    ob = rows[0]
    assert ob.estimated and ob.txn_type == "contribution" and ob.trade_date == date(2026, 4, 1) and ob.amount == Decimal(1_793_516)
    contrib = [r for r in rows if not r.estimated]
    assert len(contrib) == 6 and all(r.amount == Decimal(41_250) for r in contrib)  # employee 21,250 + employer 20,000
    assert contrib[3].trade_date == contrib[4].trade_date == date(2026, 8, 15)
    assert len({r.fingerprint for r in rows}) == len(rows)  # two credits on one day stay two entries
    assert meta["pension"] == 61_250  # reported, not imported
    assert not issues  # entries add up to the passbook's own closing balance (12,22,647 + 8,18,369)


def test_a_mismatch_with_the_closing_balance_is_flagged():
    _, issues, _ = epf.parse(passbook(closing=("12,00,000", "8,00,000", "0")))
    assert any("closing balance" in i["error"] for i in issues)
