"""Synthetic broker exports (made-up numbers, no real person's data) shaped like each broker's download:
title / client rows above the table, the broker's own column names, its date format and buy/sell words.
Layouts follow the brokers' published report descriptions; a layout that drifts is caught by the
"map the columns once" fallback in the app."""
from __future__ import annotations

import io
from typing import Any


def xlsx(rows: list[list[Any]], sheet: str = "Sheet1") -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def csv(rows: list[list[Any]]) -> bytes:
    import csv as _csv

    buf = io.StringIO()
    _csv.writer(buf).writerows(rows)
    return buf.getvalue().encode()


# ---- ICICI Direct: Portfolio → Stocks → Download → "All Transaction" (CSV). Symbols are ICICI's own codes.
ICICI_TRADES = csv([
    ["Stock Symbol", "Company Name", "ISIN Code", "Action", "Quantity", "Transaction Price", "Brokerage", "Transaction Charges",
     "StampDuty", "Segment", "STT Paid/Not Paid", "Remarks", "Transaction Date", "Exchange"],
    ["RELIND", "RELIANCE INDUSTRIES LTD", "INE002A01018", "Buy", "10", "2400.50", "12.00", "3.10", "3.60", "Equity", "Paid", "", "15-Jan-2025", "NSE"],
    ["INFTEC", "INFOSYS LTD", "INE009A01021", "Buy", "5", "1500.00", "7.50", "1.90", "1.10", "Equity", "Paid", "", "20-Feb-2025", "NSE"],
    ["RELIND", "RELIANCE INDUSTRIES LTD", "INE002A01018", "Sell", "4", "2600.00", "10.40", "1.20", "0.00", "Equity", "Paid", "", "10-Jun-2025", "NSE"],
])

# ---- ICICI Direct: Portfolio → Stocks → Download → "Summary" (holdings)
ICICI_HOLDINGS = csv([
    ["Stock Symbol", "Company Name", "ISIN Code", "Qty", "Average Cost Price", "Current Market Price", "% Change over prev close",
     "Value At Cost", "Value At Market Price", "Realized Profit / Loss", "Unrealized Profit/Loss", "Unrealized Profit / Loss %"],
    ["RELIND", "RELIANCE INDUSTRIES LTD", "INE002A01018", "6", "2400.50", "2750.00", "0.5", "14403.00", "16500.00", "800.00", "2097.00", "14.56"],
    ["INFTEC", "INFOSYS LTD", "INE009A01021", "5", "1500.00", "1450.00", "-0.2", "7500.00", "7250.00", "0.00", "-250.00", "-3.33"],
])

# ---- Upstox: Reports → Trade (XLSX) — title and client rows above the table, BSE scrip codes
UPSTOX_TRADES = xlsx([
    ["Trade Report"],
    ["Client Code", "XY1234"],
    ["Period", "01-04-2025 to 31-03-2026"],
    [],
    ["Date", "Company", "Scrip Code", "Exchange", "Side", "Quantity", "Price", "Amount", "Trade Num", "Trade Time", "Segment"],
    ["2025-05-02", "RELIANCE INDUSTRIES LTD", "500325", "BSE", "Buy", 3, 2410.0, 7230.0, "1234567", "10:15:01", "EQ"],
    ["2025-05-02", "TATA POWER CO LTD", "TATAPOWER", "NSE", "Buy", 20, 380.25, 7605.0, "1234568", "10:20:44", "EQ"],
    ["2025-06-12", "NIFTY 26JUN25 24000 CE", "NIFTY", "NFO", "Buy", 75, 110.0, 8250.0, "1234569", "11:00:00", "FO"],
    ["2025-07-01", "TATA POWER CO LTD", "TATAPOWER", "NSE", "Sell", 5, 410.0, 2050.0, "1234570", "14:00:00", "EQ"],
], sheet="Trades")

# ---- Paytm Money: Stocks → Holdings download (XLSX)
PAYTM_HOLDINGS = xlsx([
    ["Holdings Statement"],
    ["Name", "TEST USER"],
    [],
    ["Stock Name", "ISIN", "Quantity", "Avg. Buy Price (₹)", "Invested Value (₹)", "LTP (₹)", "Current Value (₹)", "Overall P&L (₹)"],
    ["ITC LTD", "INE154A01025", 40, 430.5, 17220.0, 415.0, 16600.0, -620.0],
    ["NIPPON INDIA ETF NIFTY 50 BEES", "INF204KB14I2", 100, 250.0, 25000.0, 262.0, 26200.0, 1200.0],
])

# ---- Groww: Stocks → Holdings → Download (XLSX)
GROWW_HOLDINGS = xlsx([
    ["Name", "TEST USER"],
    ["Unique Client Code", "1234567890"],
    [],
    ["Holdings as on 30-09-2026"],
    ["Stock Name", "ISIN", "Quantity", "Average buy price", "Buy value", "Closing price", "Closing value", "Unrealised P&L"],
    ["HDFC BANK LTD", "INE040A01034", 8, 1600.0, 12800.0, 1720.0, 13760.0, 960.0],
])

# ---- SBI Securities: trade book (CSV), dd-mm-yyyy dates, B / S
SBI_TRADES = csv([
    ["Client Trade Book"],
    ["Trade Date", "Scrip Name", "ISIN", "Buy/Sell", "Quantity", "Rate", "Amount", "Brokerage", "Exchange"],
    ["03-03-2025", "STATE BANK OF INDIA", "INE062A01020", "B", "15", "720.40", "10806.00", "10.81", "NSE"],
    ["04-08-2025", "STATE BANK OF INDIA", "INE062A01020", "S", "5", "810.00", "4050.00", "4.05", "NSE"],
])

# ---- Kotak Securities: holdings (CSV)
KOTAK_HOLDINGS = csv([
    ["Security Name", "ISIN", "Qty", "Avg Cost Price", "Market Price", "Market Value", "Unrealised P&L"],
    ["LARSEN & TOUBRO LTD", "INE018A01030", "4", "3300.00", "3600.00", "14400.00", "1200.00"],
])

# ---- A layout no reader knows (a hand-made or unusual export) → "map the columns once"
ODD_TRADES = csv([
    ["Particular", "Dt", "B-S", "Nos", "Px", "Code"],
    ["WIPRO LTD", "11/03/2025", "BUY", "12", "480.00", "INE075A01022"],
    ["WIPRO LTD", "12/09/2025", "SELL", "2", "520.00", "INE075A01022"],
])

# ---- Kotak Securities: Tax P&L (Excel) — "Script Name", purchase / sale columns, a title row
KOTAK_TAX = xlsx([
    ["Capital Gain Statement for FY 2025-26"],
    [],
    ["Equity - Short Term"],
    ["Script Name", "ISIN", "Quantity", "Purchase Date", "Purchase Price", "Purchase Value", "Sale Date", "Sale Price", "Sale Value", "Profit/Loss"],
    ["INFOSYS LTD", "INE009A01021", 5, "02-04-2025", 1500.0, 7500.0, "15-09-2025", 1400.0, 7000.0, -500.0],
    ["ITC LTD", "INE154A01025", 10, "10-05-2025", 420.0, 4200.0, "20-12-2025", 450.0, 4500.0, 300.0],
], sheet="Capital Gains")
