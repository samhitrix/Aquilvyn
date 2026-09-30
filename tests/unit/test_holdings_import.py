"""Holdings statements (Zerodha Console XLSX, Kite CSV) → positions; AMFI master; Yahoo quotes."""
import io
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from portfolio_svc.importers import detect_kind
from portfolio_svc.importers.holdings import parse

openpyxl = pytest.importorskip("openpyxl")


def _zerodha_xlsx() -> bytes:
    wb = openpyxl.Workbook()
    eq = wb.active
    eq.title = "Equity"
    eq.append(["Client ID", "AB1234"])
    eq.append([])
    eq.append(["Summary"])
    eq.append([])
    eq.append(["Symbol", "ISIN", "Sector", "Quantity Available", "Quantity Discrepant", "Quantity Long Term",
               "Quantity Pledged (Margin)", "Quantity Pledged (Loan)", "Average Price", "Previous Closing Price",
               "Unrealized P&L", "Unrealized P&L Pct."])
    eq.append(["STYRENIX", "INE189B01011", "Chemicals", 85, 0, 60, 0, 0, 2298.84, 2900.5, 51141.1, 26.17])
    # 10 pledged: available excludes them — the P&L column tells us the true total is 30
    eq.append(["NIFTYBEES", "INF204KB14I2", "ETF", 20, 0, 0, 10, 0, 250.0, 270.0, 600.0, 8.0])
    mf = wb.create_sheet("Mutual Funds")
    mf.append(["Symbol", "ISIN", "Instrument Type", "Quantity Available", "Quantity Discrepant", "Quantity Long Term",
               "Quantity Pledged (Margin)", "Quantity Pledged (Loan)", "Average Price", "Previous Closing Price",
               "Unrealized P&L", "Unrealized P&L Pct."])
    mf.append(["MOTILAL OSWAL MIDCAP FUND - DIRECT PLAN", "INF247L01445", "Equity", 1261.396, 0, 0, 0, 0, 111.39, 140.2, 36340.8, 25.8])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_zerodha_console_xlsx_both_sheets():
    content = _zerodha_xlsx()
    assert detect_kind("holdings.xlsx", content) == "holdings"
    rows, errs = parse(content)
    assert not errs, errs
    by = {r.name: r for r in rows}
    s = by["STYRENIX"]
    assert s.asset_type == "stock" and s.symbol == "STYRENIX" and s.quantity == 85 and s.long_term_qty == 60
    assert s.invested == (Decimal("85") * Decimal("2298.84")).quantize(Decimal("0.01")) and s.price == Decimal("2900.5")
    assert by["NIFTYBEES"].asset_type == "etf" and by["NIFTYBEES"].quantity == 30  # pledged units counted
    m = by["MOTILAL OSWAL MIDCAP FUND - DIRECT PLAN"]
    assert m.asset_type == "mutual_fund" and m.symbol is None and m.isin == "INF247L01445" and m.quantity == Decimal("1261.396")


def test_kite_holdings_csv():
    csv = b'"Instrument","Qty.","Avg. cost","LTP","Cur. val","P&L","Net chg.","Day chg."\n"DMART","10","4246.00","4400.1","44001","1541","3.63","0.5"\n'
    assert detect_kind("holdings.csv", csv) == "holdings"
    rows, errs = parse(csv)
    assert not errs and rows[0].symbol == "DMART" and rows[0].quantity == 10 and rows[0].avg_price == Decimal("4246.00")


def test_tradebook_is_not_mistaken_for_holdings():
    csv = b"symbol,trade_date,trade_type,quantity,price\nTCS,2024-01-01,buy,1,3500\n"
    assert detect_kind("x.csv", csv) == "trades"


def test_amfi_master_maps_both_isins():
    from market_svc.providers.amfi import parse_navall

    text = ("Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date\n\n"
            "Open Ended Schemes(Equity Scheme - Mid Cap Fund)\n\nMotilal Oswal Mutual Fund\n\n"
            "127042;INF247L01445;-;Motilal Oswal Midcap Fund-Direct Plan-Growth Option;140.2011;26-Sep-2026\n")
    m = parse_navall(text)
    assert m["INF247L01445"]["code"] == "127042" and m["INF247L01445"]["nav"] == pytest.approx(140.2011)


@pytest.mark.asyncio
async def test_yahoo_chart_quote_uses_previous_session_close(monkeypatch):
    import httpx

    from market_svc import health
    from market_svc.providers.yahoo import YahooProvider

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(health, "record", _noop)
    day = lambda d, h=9: int(datetime(2026, 9, d, h, tzinfo=UTC).timestamp())  # noqa: E731
    body = {"chart": {"result": [{
        "meta": {"regularMarketPrice": 110.0, "regularMarketTime": day(28, 9), "gmtoffset": 19800, "currency": "INR",
                 "chartPreviousClose": 90.0},
        "timestamp": [day(24, 4), day(25, 4), day(26, 4), day(28, 4)],
        "indicators": {"quote": [{"close": [95.0, 97.0, 100.0, 110.0]}]},
    }]}}
    p = YahooProvider()
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body)))
    q = (await p.quotes(["TCS.NS"]))["TCS.NS"]
    assert q.price == 110.0 and q.prev_close == 100.0  # not the 5-day window's chartPreviousClose (90)


def _with_dimension_a1(xlsx: bytes) -> bytes:
    """Zerodha's export declares the sheet size as just "A1" — openpyxl read-only mode then sees column A only."""
    import re
    import zipfile

    zin, out = zipfile.ZipFile(io.BytesIO(xlsx)), io.BytesIO()
    with zipfile.ZipFile(out, "w") as zout:
        for it in zin.infolist():
            data = zin.read(it.filename)
            if it.filename.startswith("xl/worksheets/sheet"):
                data = re.sub(rb'<dimension ref="[^"]+"/>', b'<dimension ref="A1"/>', data)
            zout.writestr(it, data)
    return out.getvalue()


def test_zerodha_single_sheet_export_real_layout():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Client ID", "XX0000"])
    ws.append([])
    ws.append(["Symbol", "ISIN", "Sector", "Instrument Type", "Quantity Available", "Quantity Discrepant", "Quantity Long Term",
               "Quantity Pledged (Margin)", "Quantity Pledged (Loan)", "Average Price", "Previous Closing Price", "Unrealized P&L",
               "Unrealize P&L Pct."])
    ws.append(["LIQUIDBEES-F", "INF732E01037", "ETF", "-", 8.635, 0, 209.192, 209, 0, 918.9364, 1000.01, 17636.1764, 8.818])
    ws.append(["BANDHAN NIFTY 50 INDEX FUND - DIRECT PLAN", "INF194K012A8", "-", "Others - Index Funds/ETFs", 1875.539, 0, "-", 0, 0,
               53.1888, 51.8552, -2501.2188, -2.5073])
    ws.append(["CANARA ROBECO LARGE CAP FUND - DIRECT PLAN", "INF760K01FR2", "-", "Equity - Large Cap", 0.989, 0, "-", 752, 0,
               59.7588, 69.87, 7613.6191, 16.92])
    buf = io.BytesIO()
    wb.save(buf)
    rows, errs = parse(_with_dimension_a1(buf.getvalue()))
    assert not errs, errs
    by = {r.name: r for r in rows}
    etf = by["LIQUIDBEES-F"]
    assert etf.asset_type == "etf" and etf.symbol == "LIQUIDBEES" and etf.quantity == Decimal("217.635") and etf.long_term_qty == Decimal("209.192")
    idx = by["BANDHAN NIFTY 50 INDEX FUND - DIRECT PLAN"]
    assert idx.asset_type == "mutual_fund" and idx.quantity == Decimal("1875.539")  # "Index Funds/ETFs" category is still a fund
    assert by["CANARA ROBECO LARGE CAP FUND - DIRECT PLAN"].quantity == Decimal("752.989")  # pledged units counted (P&L check)


@pytest.mark.asyncio
async def test_yahoo_unknown_symbol_is_a_note_not_a_failure(monkeypatch):
    import httpx

    from market_svc import health
    from market_svc.providers.yahoo import YahooProvider

    calls: list[dict] = []

    async def _rec(source, ok, error=None, count=0, note=None):
        calls.append({"ok": ok, "error": error, "note": note})

    monkeypatch.setattr(health, "record", _rec)
    p = YahooProvider()
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(404, json={"chart": {"result": None}})))
    assert await p.quotes(["ZOMATO.NS"]) == {}
    assert calls == [{"ok": None, "error": None, "note": "Not listed on Yahoo (renamed or delisted?): ZOMATO.NS"}]


def test_same_holdings_on_two_sheets_are_counted_once():
    """User report: every quantity doubled — the export lists the equity table on two sheets."""
    hdr = ["Symbol", "ISIN", "Sector", "Instrument Type", "Quantity Available", "Quantity Discrepant", "Quantity Long Term",
           "Quantity Pledged (Margin)", "Quantity Pledged (Loan)", "Average Price", "Previous Closing Price", "Unrealized P&L", "Unrealize P&L Pct."]
    data = [["SUNPHARMA", "INE044A01036", "HEALTHCARE", "-", 50, 0, 0, 0, 0, 1854, 1840, -700, -0.7551],
            ["TATAPOWER", "INE245A01021", "ENERGY", "-", 105, 0, 0, 0, 0, 405.0786, 362, -4523.25, -10.6346],
            ["TMCV", "INE1TAE01010", "AUTOMOBILE", "-", 10, 0, 10, 0, 0, 246.3031, 432.95, 1866.4695, 75.7794]]
    wb = openpyxl.Workbook()
    for title in ("Equity", "Combined"):
        ws = wb.active if title == "Equity" else wb.create_sheet(title)
        ws.title = title
        ws.append(["Client ID", "AB1234"])
        ws.append(hdr)
        for row in data:
            ws.append(row)
    # and a second copy of the table further down the same sheet
    wb["Combined"].append([])
    wb["Combined"].append(hdr)
    for row in data:
        wb["Combined"].append(row)
    buf = io.BytesIO()
    wb.save(buf)
    rows, issues = parse(buf.getvalue())
    q = {r.name: r.quantity for r in rows}
    assert q == {"SUNPHARMA": 50, "TATAPOWER": 105, "TMCV": 10}, q
    assert [i for i in issues if i.get("level") != "warning"] == []
    assert any("counted once" in i["error"] for i in issues)


def test_zerodha_combined_sheet_is_the_only_one_read_and_series_suffixes_are_stripped():
    from portfolio_svc.importers.holdings import _exchange_symbol

    hdr = ["Symbol", "ISIN", "Instrument Type", "Quantity Available", "Average Price"]
    wb = openpyxl.Workbook()
    eq = wb.active
    eq.title = "Equity"
    eq.append(hdr)
    eq.append(["GOLDBEES-E", "INF204KB17I5", "-", 281, 129.504])
    mf = wb.create_sheet("Mutual Funds")
    mf.append(hdr)
    mf.append(["AXIS LARGE CAP FUND - DIRECT PLAN", "INF846K01DP8", "Equity - Large Cap", 297.928, 68.29])
    comb = wb.create_sheet("Combined")
    comb.append(hdr)
    comb.append(["GOLDBEES-E", "INF204KB17I5", "-", 281, 129.504])
    comb.append(["AXIS LARGE CAP FUND - DIRECT PLAN", "INF846K01DP8", "Equity - Large Cap", 297.928, 68.29])
    buf = io.BytesIO()
    wb.save(buf)
    rows, issues = parse(buf.getvalue())
    assert [(r.symbol, r.asset_type, r.quantity) for r in rows] == [("GOLDBEES", "etf", Decimal("281")), (None, "mutual_fund", Decimal("297.928"))]
    assert not issues
    assert _exchange_symbol("BAJAJ-AUTO") == "BAJAJ-AUTO" and _exchange_symbol("LIQUIDCASE-F") == "LIQUIDCASE" and _exchange_symbol("ABC-BE") == "ABC"


def test_nse_size_lists_give_sebi_bands():
    from market_svc.providers.nse_index import bucket, parse_constituents

    large = parse_constituents("﻿Company Name,Industry,Symbol,Series,ISIN Code\nHDFC Bank Ltd.,Financial Services,HDFCBANK,EQ,INE040A01034\n")
    mid = parse_constituents("Company Name,Industry,Symbol,Series,ISIN Code\nBharat Electronics,Capital Goods,BEL,EQ,INE263A01024\n")
    lists = {"large": large, "mid": mid}
    assert bucket(lists, "HDFCBANK.NS", None) == "large"
    assert bucket(lists, "XYZ.NS", "INE263A01024") == "mid"          # matched by ISIN even if the symbol differs
    assert bucket(lists, "SKYWAYS.NS", "INE0PX301025") == "small"    # on NSE but in neither list
    assert bucket({}, "HDFCBANK.NS", None) is None                   # lists unavailable → caller falls back


def test_statement_sector_is_kept_for_stocks_only():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Combined"
    ws.append(["Symbol", "ISIN", "Sector", "Instrument Type", "Quantity Available", "Average Price"])
    ws.append(["BEL", "INE263A01024", "DEFENCE", "-", 135, 407.8685])
    ws.append(["NIFTYBEES", "INF204KB14I2", "ETF", "-", 198, 278.0963])
    ws.append(["AXIS LARGE CAP FUND - DIRECT PLAN", "INF846K01DP8", "-", "Equity - Large Cap", 297.928, 68.29])
    buf = io.BytesIO()
    wb.save(buf)
    rows, _ = parse(buf.getvalue())
    assert [(r.symbol, r.sector) for r in rows] == [("BEL", "Defence"), ("NIFTYBEES", None), (None, None)]


NPS_SAMPLE = """NPS Transaction Statement for Tier I Account,,,,,,,
,,,,,,,
Subscriber Details,,,,,,,
PRAN,110000000001,,,,,,
Subscriber Name,TEST USER,,,,,,
,,,,,,,
Investment Summary,,,,,,,
Value of your Holdings(Investments)as on September 29 2026 (in Rs),No of Contributions,Total Contribution in your account as on September 29 2026 (in Rs),Total Withdrawal (in Rs),Total Notional Gain/Loss (in Rs),Charges (in Rs),Return on Investment(XIRR),9.00%
(A),,(B),(C),D=(A-B)+C,E,,
Rs 200000.00,10,Rs 150000.00,Rs 0.00,Rs 50000.00,Rs 10.00, ,
,,,,,,,
Investment Details - Scheme Wise Summary,,,,,,,
Particulars,Scheme wise Value of your Holdings(Investments) (in Rs) (E = U * N),Total Units ( U ),NAV as on 28-Sep-2026 ( N ),,,,
HDFC PENSION FUND SCHEME E - TIER I,100000,1000,100,,,,
HDFC PENSION FUND SCHEME C - TIER I,60000,1500,40,,,,
HDFC PENSION FUND SCHEME G - TIER I,40000,1000,40,,,,
,,,,,,,
Contribution/Redemption Details during the selected period,,,,,,,
Date,Particulars,Uploaded By,Employee Contribution(Rs),Employer's Contribution(Rs),Total(Rs),,
04-May-26,By Contribution,Bank (1),0,1000,1000,,
"""


def test_nps_statement_becomes_three_scheme_holdings_matching_the_totals():
    from portfolio_svc.importers import detect_kind
    from portfolio_svc.importers.nps import parse as nps_parse
    from portfolio_svc.importers.nps import statement_meta as nps_meta

    content = NPS_SAMPLE.encode()
    assert detect_kind("statement.csv", content) == "nps_statement"
    assert nps_meta(content) == {"pran": "110000000001"}
    rows, issues = nps_parse(content)
    assert [(r.symbol, r.quantity, r.price) for r in rows] == [
        ("NPS-HDFC-E-T1", Decimal("1000"), Decimal("100")), ("NPS-HDFC-C-T1", Decimal("1500"), Decimal("40")),
        ("NPS-HDFC-G-T1", Decimal("1000"), Decimal("40"))]
    assert sum(r.invested for r in rows) == Decimal("150000.00")          # = total contribution
    assert rows[0].invested == Decimal("75000.00")                         # split by value (50%)
    assert all(r.asset_type == "nps" for r in rows) and any(i.get("level") == "warning" for i in issues)
    assert all(r.price_date == date(2026, 9, 28) for r in rows)             # "NAV as on 28-Sep-2026"


def test_generic_csv_with_extra_columns_does_not_crash():
    from portfolio_svc.importers.base import read_csv

    rows = read_csv(b"symbol,qty\nTCS,1,extra,more\n")
    assert rows == [{"symbol": "TCS", "qty": "1"}]
