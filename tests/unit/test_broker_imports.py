"""Files from brokers other than Zerodha: recognised by their columns (never by the broker's name), with title
rows above the table, the broker's own words for buy/sell and its date format; anything unrecognised is
mapped once by the person and remembered."""
from datetime import date
from decimal import Decimal

from portfolio_svc.importers import holdings, mapping, trades
from portfolio_svc.importers.base import detect_kind
from tests.fixtures import broker_files as bf


def by_symbol(rows):
    return {(r.symbol, r.txn_type): r for r in rows}


def test_icici_direct_transactions():
    assert detect_kind("PortfolioEquity_Transactions.csv", bf.ICICI_TRADES) == "trades"
    rows, errors = trades.parse(bf.ICICI_TRADES)
    assert not errors and len(rows) == 3
    buy = next(r for r in rows if r.txn_type == "buy" and r.isin == "INE002A01018")
    # ICICI's own code is kept as the symbol here; the import swaps it for the NSE symbol via the ISIN
    assert (buy.symbol, buy.name, buy.trade_date, buy.quantity, buy.price) == ("RELIND", "RELIANCE INDUSTRIES LTD", date(2025, 1, 15), 10, Decimal("2400.50"))
    assert buy.asset_type == "stock" and buy.exchange == "NSE"
    assert next(r for r in rows if r.txn_type == "sell").quantity == 4


def test_icici_direct_holdings_summary():
    assert detect_kind("PortfolioEquity_Summary.csv", bf.ICICI_HOLDINGS) == "holdings"
    rows, errors = holdings.parse(bf.ICICI_HOLDINGS)
    assert not [e for e in errors if e.get("level") != "warning"]
    rel = next(r for r in rows if r.isin == "INE002A01018")
    assert (rel.quantity, rel.avg_price, rel.invested) == (6, Decimal("2400.50"), Decimal("14403.00"))


def test_upstox_trades_xlsx_with_title_rows_bse_codes_and_fno_skipped():
    assert detect_kind("trade_report.xlsx", bf.UPSTOX_TRADES) == "trades"
    rows, errors = trades.parse(bf.UPSTOX_TRADES)
    syms = by_symbol(rows)
    assert set(syms) == {("500325", "buy"), ("TATAPOWER", "buy"), ("TATAPOWER", "sell")}
    rel = syms[("500325", "buy")]
    assert rel.exchange == "BSE" and rel.asset_type == "stock" and rel.yahoo_symbol == "500325.BO"  # a BSE scrip code, not a fund
    assert rel.name == "RELIANCE INDUSTRIES LTD" and rel.external_id.startswith("1234567|")
    assert any("futures & options" in e["error"] for e in errors)  # the NIFTY option is not an investment


def test_paytm_and_groww_holdings_with_currency_marks_and_title_rows():
    for content in (bf.PAYTM_HOLDINGS, bf.GROWW_HOLDINGS):
        assert detect_kind("holdings.xlsx", content) == "holdings"
    rows, _ = holdings.parse(bf.PAYTM_HOLDINGS)
    itc = next(r for r in rows if r.isin == "INE154A01025")
    assert (itc.quantity, itc.avg_price, itc.invested) == (40, Decimal("430.5"), Decimal("17220.0"))
    etf = next(r for r in rows if r.isin == "INF204KB14I2")
    assert etf.asset_type == "etf"  # an ETF (INF… ISIN) is not a mutual fund
    rows, _ = holdings.parse(bf.GROWW_HOLDINGS)
    assert rows[0].isin == "INE040A01034" and rows[0].quantity == 8 and rows[0].avg_price == 1600


def test_sbi_trades_and_kotak_holdings():
    rows, errors = trades.parse(bf.SBI_TRADES)
    assert not errors and [(r.txn_type, r.quantity, r.trade_date) for r in rows] == [("buy", 15, date(2025, 3, 3)), ("sell", 5, date(2025, 8, 4))]
    assert rows[0].symbol == "STATE BANK OF INDIA" and rows[0].isin == "INE062A01020"  # the name; the ISIN gives the NSE symbol on import
    rows, _ = holdings.parse(bf.KOTAK_HOLDINGS)
    assert rows[0].isin == "INE018A01030" and rows[0].avg_price == 3300


def test_an_unknown_layout_is_mapped_once():
    assert detect_kind("export.csv", bf.ODD_TRADES) == "unknown"
    cand = mapping.candidate(bf.ODD_TRADES)
    assert cand["headers"] == ["Particular", "Dt", "B-S", "Nos", "Px", "Code"] and len(cand["samples"]) == 2
    fields = {"date": "Dt", "side": "B-S", "symbol": "Particular", "qty": "Nos", "price": "Px", "isin": "Code"}
    assert mapping.validate("trades", {"date": "Dt"})  # incomplete mappings are refused, with the reason
    rows, errors = mapping.apply(bf.ODD_TRADES, "trades", fields)
    assert not errors and [(r.txn_type, r.quantity, r.price, r.isin) for r in rows] == [
        ("buy", 12, Decimal("480.00"), "INE075A01022"), ("sell", 2, Decimal("520.00"), "INE075A01022")]
    # the same layout is recognised by its header fingerprint next time (column names only)
    assert mapping.fingerprint(cand["headers"]) == cand["fingerprint"]


def test_isin_list_parsing():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "market"))
    from market_svc.providers.isin import ISIN_RE, parse_equity_list

    text = ("SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\n"
            "RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10,1,INE002A01018,10\n"
            "BAD,Broken row,EQ,01-JAN-2000,10,1,NOTANISIN,10\n")
    assert parse_equity_list(text) == {"INE002A01018": {"symbol": "RELIANCE", "name": "Reliance Industries Limited"}}
    assert ISIN_RE.match("INF204KB14I2") and not ISIN_RE.match("US0378331005")


def test_kotak_tax_pnl():
    from portfolio_svc.importers import taxpnl

    assert detect_kind("capital_gains.xlsx", bf.KOTAK_TAX) == "tax_pnl"
    rows, errors, meta = taxpnl.parse(bf.KOTAK_TAX)
    gains = [r for r in rows if r.record == "gain"]
    assert [(r.symbol, r.quantity, r.profit, r.term) for r in gains] == [("INFOSYS LTD", 5, -500, "short"), ("ITC LTD", 10, 300, "short")]
    assert not [e for e in errors if e.get("level") != "warning"]


def test_mapping_guesses_from_values_when_names_say_nothing():
    cand = mapping.candidate(bf.ODD_TRADES)
    assert cand["kind"] == "trades"  # BUY / SELL values give it away
    g = cand["suggested"]["trades"]
    assert (g.get("isin"), g.get("side"), g.get("date"), g.get("symbol"), g.get("qty"), g.get("price")) == ("Code", "B-S", "Dt", "Particular", "Nos", "Px")


def test_depository_statement_is_a_holdings_check_across_brokers():
    """An NSDL/CDSL eCAS has quantities but no cost: it's compared with what Aquilvyn holds, never imported."""
    from decimal import Decimal as D

    from portfolio_svc.depository import is_depository, reconcile

    ecas = {"statement_period": {"from": "01-Aug-2026", "to": "31-Aug-2026"}, "accounts": [
        {"name": "ZERODHA BROKING LIMITED", "type": "CDSL", "client_id": "1208160012345678", "balance": "60000",
         "owners": [{"name": "TEST PERSON", "pan": "ABCDE1234F"}],
         "equities": [{"isin": "INE002A01018", "name": "RELIANCE INDUSTRIES", "num_shares": "10", "price": "2800", "value": "28000"},
                      {"isin": "INE009A01021", "name": "INFOSYS", "num_shares": "8", "price": "1500", "value": "12000"}],
         "mutual_funds": []},
        {"name": "ICICI SECURITIES LIMITED", "type": "NSDL", "client_id": "10012345", "balance": "20000",
         "owners": [{"name": "TEST PERSON", "pan": "ABCDE1234F"}],
         "equities": [{"isin": "INE040A01034", "name": "HDFC BANK", "num_shares": "12", "price": "1650", "value": "19800"}], "mutual_funds": []},
    ]}
    assert is_depository(ecas) and not is_depository({"folios": [{}], "accounts": []})
    held = {"p1": {"INE002A01018": D(10), "INE009A01021": D(5), "INE154A01025": D(40)}}  # Infosys short, ITC not at the depository
    rep = reconcile(ecas, held, {"ABCDE1234F": {"id": "p1", "name": "Test Person"}}, {"INE154A01025": "ITC LTD"})
    status = {ln["isin"]: ln["status"] for a in rep["accounts"] for ln in a["lines"]}
    assert status == {"INE002A01018": "ok", "INE009A01021": "differs", "INE040A01034": "missing"}
    assert [e["isin"] for e in rep["extra"]] == ["INE154A01025"]
    assert rep["counts"] == {"ok": 1, "differs": 1, "missing": 1, "extra": 1}
    assert rep["accounts"][0]["broker"] == "Zerodha Broking Limited" and rep["accounts"][0]["client"].endswith("678")
    assert "ABCDE1234F" not in str(rep)  # the PAN is only ever shown masked
    # a PAN that isn't in the family is reported, not guessed
    rep = reconcile(ecas, held, {})
    assert rep["unmatched_pans"] and all(ln["status"] == "missing" for a in rep["accounts"] for ln in a["lines"])
