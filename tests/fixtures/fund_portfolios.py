"""Synthetic fund-house monthly portfolio files (made-up weights, real public ISINs of listed companies),
shaped like the layouts AMCs publish: title rows above the table, a sheet per scheme, section rows without
ISINs, TREPS / net-current-asset lines, sub-totals — and the variations between houses."""
from __future__ import annotations

import io
import zipfile
from datetime import datetime
from typing import Any

from tests.fixtures.broker_files import csv, xlsx

RELIANCE, HDFCBANK, ICICIBANK, INFY, TCS, ITC, LT, AXIS = (
    "INE002A01018", "INE040A01034", "INE090A01021", "INE009A01021", "INE467B01029", "INE154A01025", "INE018A01030", "INE238A01034")
GSEC, CORP_BOND = "IN0020230085", "INE001A07TQ5"


def _workbook(sheets: dict[str, list[list[Any]]]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


HEADER_A = ["Name of Instrument", "ISIN", "Coupon (%)", "Industry+ /Rating", "Quantity", "Market/ Fair Value (Rs. in Lacs.)", "% to NAV"]

# ---- "HDFC-like": one workbook, a sheet per scheme, "SCHEME NAME :" label + value, date as text
AMC_A = _workbook({
    "FLEXI": [
        ["HDFC Mutual Fund"],
        ["SCHEME NAME :", "HDFC Flexi Cap Fund (An open ended dynamic equity scheme investing across large cap, mid cap, small cap stocks)"],
        ["PORTFOLIO STATEMENT AS ON :", "July 31, 2026"],
        [],
        HEADER_A,
        ["EQUITY & EQUITY RELATED"],
        ["ICICI Bank Ltd.", ICICIBANK, None, "Banks", 1000, 15000.5, 9.82],
        ["HDFC Bank Ltd.", HDFCBANK, None, "Banks", 900, 13000.0, 8.51],
        ["Axis Bank Ltd.", AXIS, None, "Banks", 800, 11000.0, 7.20],
        ["Infosys Limited", INFY, None, "IT - Software", 500, 6000.0, 3.93],
        ["Sub Total", None, None, None, None, None, 29.46],
        ["DEBT INSTRUMENTS"],
        ["7.18% GOI 2033", GSEC, 7.18, "Sovereign", 100, 1000.0, 0.65],
        ["TREPS - Tri-party Repo", None, None, None, None, 9000.0, 5.89],
        ["Net Current Assets", None, None, None, None, -200.0, -0.13],
        ["Grand Total", None, None, None, None, None, 100.0],
    ],
    "TOP100": [
        ["HDFC Mutual Fund"],
        ["SCHEME NAME :", "HDFC Large Cap Fund (Formerly known as HDFC Top 100 Fund) (An open ended equity scheme)"],
        ["PORTFOLIO STATEMENT AS ON :", "July 31, 2026"],
        HEADER_A,
        ["ICICI Bank Ltd.", ICICIBANK, None, "Banks", 1000, 15000.5, 10.1],
        ["HDFC Bank Ltd.", HDFCBANK, None, "Banks", 900, 13000.0, 9.9],
        ["Reliance Industries Ltd.", RELIANCE, None, "Petroleum Products", 700, 9000.0, 8.0],
        ["Larsen & Toubro Ltd.", LT, None, "Construction", 300, 5000.0, 4.0],
    ],
})

# ---- "ICICI-like": scheme name alone in row 1, dotted date, "% to Nav" as a FRACTION, different column words
HEADER_B = ["Company/Issuer/Instrument Name", "ISIN", "Coupon", "Industry/Rating", "Quantity", "Exposure/Market Value(Rs.Lakh)", "% to Nav"]
AMC_B = _workbook({
    "BLUECHIP": [
        ["ICICI Prudential Large Cap Fund (erstwhile ICICI Prudential Bluechip Fund) (An open Ended Equity Scheme)"],
        ["Portfolio as on 31.07.2026"],
        [],
        HEADER_B,
        ["Equity Shares"],
        ["HDFC Bank Ltd.", HDFCBANK, None, "Banks", 100, 1000.0, 0.0950],
        ["ICICI Bank Ltd.", ICICIBANK, None, "Banks", 100, 1000.0, 0.0880],
        ["Reliance Industries Ltd.", RELIANCE, None, "Petroleum Products", 100, 1000.0, 0.0700],
        ["Infosys Ltd.", INFY, None, "IT - Software", 100, 1000.0, 0.0450],
        ["Tata Consultancy Services Ltd.", TCS, None, "IT - Software", 100, 1000.0, 0.0400],
        ["Corporate bond", CORP_BOND, 7.5, "CRISIL AAA", 10, 100.0, 0.0050],
        ["Net Current Assets", None, None, None, None, 100.0, 0.5570],
    ],
})

# ---- several schemes stacked in ONE sheet, each with its own title + header
AMC_C_STACKED = xlsx([
    ["Example Mutual Fund — Monthly portfolio for the month ended 31-Jul-2026"],
    ["Example Nifty 50 Index Fund"],
    ["Name of the Instrument / Issuer", "ISIN", "Rating / Industry^", "Quantity", "Market value (Rs. in Lakhs)", "% to AUM"],
    ["HDFC Bank Limited", HDFCBANK, "Banks", 10, 100, 13.1],
    ["Reliance Industries Limited", RELIANCE, "Petroleum Products", 10, 100, 9.0],
    ["ICICI Bank Limited", ICICIBANK, "Banks", 10, 100, 8.6],
    [],
    ["Example Nifty Next 50 Index Fund"],
    ["Name of the Instrument / Issuer", "ISIN", "Rating / Industry^", "Quantity", "Market value (Rs. in Lakhs)", "% to AUM"],
    ["ITC Limited", ITC, "Diversified FMCG", 10, 100, 4.2],
    ["Larsen & Toubro Limited", LT, "Construction", 10, 100, 3.1],
])

# ---- "PPFAS-like": one CSV per scheme, foreign shares with US ISINs, "% of Net Assets"
AMC_D_CSV = csv([
    ["Parag Parikh Flexi Cap Fund"],
    ["Monthly Portfolio Statement as on July 31 2026"],
    ["Name of the Instrument", "ISIN", "Industry", "Quantity", "Market Value (Rs. in Lakhs)", "% of Net Assets"],
    ["HDFC Bank Limited", HDFCBANK, "Banks", "1,000", "5,000.00", "8.12%"],
    ["ITC Limited", ITC, "Diversified FMCG", "1,000", "4,000.00", "6.10%"],
    ["Alphabet Inc A", "US02079K3059", "Interactive Media", "100", "3,000.00", "4.20%"],
    ["Microsoft Corp", "US5949181045", "Software", "100", "2,500.00", "3.10%"],
])


def zipped(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, b in files.items():
            z.writestr(n, b)
    return buf.getvalue()


AMC_ZIP = zipped({"Monthly Portfolio July 2026/FLEXI.xlsx": AMC_A, "Monthly Portfolio July 2026/BLUECHIP.xlsx": AMC_B})
AS_OF = datetime(2026, 7, 31).date()

# AMFI scheme master names (what instruments are keyed by) for the funds a family holds
TARGETS = {
    "118955": {"name": "HDFC Flexi Cap Fund - Growth Option - Direct Plan", "amc": "HDFC Mutual Fund"},
    "119018": {"name": "HDFC Large Cap Fund - Growth Option - Direct Plan", "amc": "HDFC Mutual Fund"},
    "120586": {"name": "ICICI Prudential Large Cap Fund (erstwhile Bluechip Fund) - Direct Plan - Growth", "amc": "ICICI Prudential Mutual Fund"},
    "122639": {"name": "Parag Parikh Flexi Cap Fund - Direct Plan - Growth", "amc": "PPFAS Mutual Fund"},
}
