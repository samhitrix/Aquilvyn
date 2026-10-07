"""Fund-house portfolio files → per-scheme stock holdings, whatever the layout (E16 look-through)."""
from datetime import date

import pytest

from market_svc.fund_portfolio import isin_kind, match_schemes, parse_date_text, parse_portfolio
from tests.fixtures import fund_portfolios as fp


def by_scheme(schemes):
    return {s["scheme"]: s for s in schemes}


def test_sheet_per_scheme_with_label_titles_sections_and_totals():
    schemes = by_scheme(parse_portfolio(fp.AMC_A, "monthly.xlsx"))
    assert set(schemes) == {"HDFC Flexi Cap Fund", "HDFC Large Cap Fund"}
    flexi = schemes["HDFC Flexi Cap Fund"]
    assert flexi["as_of"] == fp.AS_OF
    isins = {h["isin"]: h for h in flexi["holdings"]}
    # section rows, sub-totals, TREPS and net current assets (no ISIN) are not holdings
    assert set(isins) == {fp.ICICIBANK, fp.HDFCBANK, fp.AXIS, fp.INFY, fp.GSEC}
    assert isins[fp.ICICIBANK]["weight"] == 9.82 and isins[fp.ICICIBANK]["kind"] == "equity"
    assert isins[fp.GSEC]["kind"] == "debt"
    assert flexi["equity_pct"] == pytest.approx(29.46)


def test_fraction_weights_dotted_date_and_erstwhile_names():
    s = parse_portfolio(fp.AMC_B, "icici.xlsx")[0]
    assert s["scheme"] == "ICICI Prudential Large Cap Fund" and s["as_of"] == fp.AS_OF
    w = {h["isin"]: h["weight"] for h in s["holdings"]}
    assert w[fp.HDFCBANK] == pytest.approx(9.5)  # 0.0950 of NAV → 9.5 %
    assert next(h for h in s["holdings"] if h["isin"] == fp.CORP_BOND)["kind"] == "debt"


def test_several_schemes_stacked_in_one_sheet():
    schemes = by_scheme(parse_portfolio(fp.AMC_C_STACKED, "all.xlsx"))
    assert set(schemes) == {"Example Nifty 50 Index Fund", "Example Nifty Next 50 Index Fund"}
    assert {h["isin"] for h in schemes["Example Nifty Next 50 Index Fund"]["holdings"]} == {fp.ITC, fp.LT}
    assert all(s["as_of"] == fp.AS_OF for s in schemes.values())


def test_csv_with_foreign_shares_and_percent_signs():
    s = parse_portfolio(fp.AMC_D_CSV, "ppfas.csv")[0]
    kinds = {h["isin"]: h["kind"] for h in s["holdings"]}
    assert kinds["US02079K3059"] == "foreign_equity" and kinds[fp.HDFCBANK] == "equity"
    assert s["equity_pct"] == pytest.approx(21.52)
    assert s["as_of"] == date(2026, 7, 31)


def test_zip_of_workbooks():
    names = {s["scheme"] for s in parse_portfolio(fp.AMC_ZIP, "portfolio.zip")}
    assert {"HDFC Flexi Cap Fund", "HDFC Large Cap Fund", "ICICI Prudential Large Cap Fund"} <= names


def test_sheets_are_matched_to_amfi_schemes_not_confused_with_lookalikes():
    schemes = parse_portfolio(fp.AMC_ZIP, "portfolio.zip") + parse_portfolio(fp.AMC_D_CSV, "ppfas.csv") + parse_portfolio(fp.AMC_C_STACKED, "x.xlsx")
    m = match_schemes(schemes, fp.TARGETS)
    got = {code: schemes[v["index"]]["scheme"] for code, v in m.items()}
    assert got == {"118955": "HDFC Flexi Cap Fund", "119018": "HDFC Large Cap Fund",
                   "120586": "ICICI Prudential Large Cap Fund", "122639": "Parag Parikh Flexi Cap Fund"}
    # Nifty 50 vs Nifty Next 50 must not be mixed up
    idx = match_schemes(schemes, {"1": {"name": "Example Nifty Next 50 Index Fund - Direct Growth", "amc": "Example Mutual Fund"}})
    assert schemes[idx["1"]["index"]]["scheme"] == "Example Nifty Next 50 Index Fund"
    # an unrelated fund is not forced onto the nearest sheet
    assert match_schemes(schemes, {"9": {"name": "SBI Gilt Fund - Direct Growth", "amc": "SBI Mutual Fund"}}) == {}


@pytest.mark.parametrize("text,expected", [
    ("PORTFOLIO STATEMENT AS ON : July 31, 2026", date(2026, 7, 31)),
    ("Portfolio as on 31st July 2026", date(2026, 7, 31)),
    ("as on 31-Jul-2026", date(2026, 7, 31)),
    ("Portfolio as on 31.07.2026", date(2026, 7, 31)),
    ("Monthly portfolio as on July 2026", date(2026, 7, 31)),
    ("Monthly Portfolio Statement as on April 30,2024", date(2024, 4, 30)),
    ("Portfolio as on 30-Apr-24", date(2024, 4, 30)),
    ("Portfolio as of 2026-07-31", date(2026, 7, 31)),
    ("no date here", None),
])
def test_dates(text, expected):
    assert parse_date_text(text) == expected


def test_isin_kinds():
    assert isin_kind(fp.RELIANCE) == "equity" and isin_kind(fp.CORP_BOND) == "debt" and isin_kind(fp.GSEC) == "debt"
    assert isin_kind("INF204KB14I2") == "fund" and isin_kind("US5949181045") == "foreign_equity"
