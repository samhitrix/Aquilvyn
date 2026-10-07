"""Finding and downloading a fund house's latest monthly portfolio — against a fake web, offline."""
import asyncio
from datetime import date

from market_svc.fund_sources import extract_links, fetch_amc, link_period, rank_links
from tests.fixtures import fund_portfolios as fp

AMC_PAGE = """
<html><body>
  <a href="/docs/Monthly%20Portfolio%20-%20June%202026.xlsx">Monthly Portfolio – June 2026</a>
  <a href="/docs/Monthly-Portfolio-July-2026.xlsx">Monthly Portfolio – July 2026</a>
  <a href="/docs/Fortnightly-Portfolio-15-July-2026.xlsx">Fortnightly portfolio (debt) 15 July 2026</a>
  <a href="/docs/Factsheet-July-2026.pdf">Factsheet</a>
  <a href="/docs/Riskometer-July-2026.xlsx">Riskometer</a>
  <script>window.__DATA__={"files":[{"url":"https:\\/\\/cdn.example.com\\/portfolio\\/annual-portfolio-2026.xlsx"}]}</script>
</body></html>"""

AMFI_DIR = """<html><a href="https://ppfas.example/portfolio-disclosure">PPFAS Mutual Fund</a>
<a href="https://other.example/p">Some Other Mutual Fund</a></html>"""
PPFAS_PAGE = '<a href="https://ppfas.example/files/PPFCF_PPFAS_Monthly_Portfolio_Report_July_31_2026.csv">Parag Parikh Flexi Cap Fund</a>'


def fake_web(pages: dict[str, bytes]):
    async def fetch(url: str):
        if url in pages:
            return 200, pages[url], url, "text/html"
        return 404, b"not found", url, "text/html"
    return fetch


SOURCES = {
    "amfi_directory": "https://amfi.example/portfolio-disclosure",
    "amcs": [{"amc": "HDFC Mutual Fund", "match": ["hdfc"], "pages": ["https://hdfc.example/monthly-portfolio"]},
             {"amc": "Silent Mutual Fund", "match": ["silent"], "pages": ["https://silent.example/js-page"]}],
}


def test_links_are_found_ranked_newest_monthly_first_and_noise_dropped():
    links = extract_links(AMC_PAGE, "https://hdfc.example/monthly-portfolio")
    ranked = [u for u, _, _ in rank_links(links)]
    assert ranked[0] == "https://hdfc.example/docs/Monthly-Portfolio-July-2026.xlsx"
    assert ranked[1].endswith("June%202026.xlsx")
    assert not any("Fortnightly" in u or "Riskometer" in u or "annual" in u or u.endswith(".pdf") for u in ranked)


def test_periods_in_file_names():
    assert link_period("Monthly-Portfolio-July-2026.xlsx") == (2026, 7)
    assert link_period("portfolio_jul26.xlsx") == (2026, 7)
    assert link_period("Portfolio_2026-07.zip") == (2026, 7)
    assert link_period("MonthlyPortfolio_072026.xlsx") == (2026, 7)
    assert link_period("portfolio.xlsx") is None


def test_fund_house_page_to_matched_schemes():
    web = fake_web({"https://hdfc.example/monthly-portfolio": AMC_PAGE.encode(),
                    "https://hdfc.example/docs/Monthly-Portfolio-July-2026.xlsx": fp.AMC_A})
    targets = {k: v for k, v in fp.TARGETS.items() if v["amc"].startswith("HDFC")}
    r = asyncio.run(fetch_amc("HDFC Mutual Fund", targets, web, today=date(2026, 8, 20), sources=SOURCES))
    assert r["ok"] and r["url"].endswith("July-2026.xlsx") and r["as_of"] == fp.AS_OF
    assert set(r["matched"]) == {"118955", "119018"} and r["unmatched"] == []
    assert r["matched"]["118955"]["scheme"] == "HDFC Flexi Cap Fund"


def test_unlisted_fund_house_is_found_through_amfis_directory():
    web = fake_web({"https://amfi.example/portfolio-disclosure": AMFI_DIR.encode(),
                    "https://ppfas.example/portfolio-disclosure": PPFAS_PAGE.encode(),
                    "https://ppfas.example/files/PPFCF_PPFAS_Monthly_Portfolio_Report_July_31_2026.csv": fp.AMC_D_CSV})
    r = asyncio.run(fetch_amc("PPFAS Mutual Fund", {"122639": fp.TARGETS["122639"]}, web, sources=SOURCES))
    assert r["ok"] and set(r["matched"]) == {"122639"}
    assert any(t["step"] == "AMFI directory" for t in r["tried"])


def test_a_javascript_page_says_what_to_do_instead_of_failing_silently():
    web = fake_web({"https://silent.example/js-page": b"<html><div id=root></div><script src=app.js></script></html>"})
    r = asyncio.run(fetch_amc("Silent Mutual Fund", {"1": {"name": "Silent Equity Fund", "amc": "Silent Mutual Fund"}}, web, sources=SOURCES))
    assert not r["ok"] and "fund_sources.json" in r["error"]
    assert r["tried"][0]["step"] == "page" and r["tried"][0]["files_found"] == 0


def test_a_web_page_served_instead_of_the_file_is_not_parsed():
    web = fake_web({"https://hdfc.example/monthly-portfolio": AMC_PAGE.encode(),
                    "https://hdfc.example/docs/Monthly-Portfolio-July-2026.xlsx": b"<!DOCTYPE html><html>login</html>"})
    r = asyncio.run(fetch_amc("HDFC Mutual Fund", {"118955": fp.TARGETS["118955"]}, web, sources=SOURCES))
    assert not r["ok"]
    assert any(t.get("error") == "got a web page, not a spreadsheet" for t in r["tried"])


def test_laptop_run_file_names():
    # PPFAS picked December 2025 over newer files: "December_31" was read as December 2031
    assert link_period("PPFAS_Monthly_Portfolio_Report_December_31_2025.xls") == (2025, 12)
    assert link_period("NIMF-MONTHLY-PORTFOLIO-APRIL-2024.xls") == (2024, 4)
    assert link_period("Monthly%20Portfolio%20September%202026.xlsx") == (2026, 9)
    assert link_period("Portfolio_31072026.xlsx") == (2026, 7)
    ranked = rank_links([("https://x/PPFAS_Monthly_Portfolio_Report_December_31_2025.xls", ""),
                         ("https://x/PPFAS_Monthly_Portfolio_Report_August_31_2026.xls", ""),
                         ("https://files.example/2026-09/PortfolioOverlap31Aug2026_0.xlsx", ""),  # HDFC: not a portfolio
                         ("https://x/Monthly-Portfolio-Top-10-Holdings-Aug-2026.xlsx", "")])
    assert [u.rsplit("/", 1)[-1] for u, _, _ in ranked] == ["PPFAS_Monthly_Portfolio_Report_August_31_2026.xls",
                                                            "PPFAS_Monthly_Portfolio_Report_December_31_2025.xls"]


def test_holdings_api_fills_in_for_a_javascript_only_fund_house():
    import json

    from market_svc.fund_api import fetch_scheme

    scheme = {"data": {"amfi_code": "120503", "name": "Axis ELSS Tax Saver Fund - Direct Growth", "family_id": 42}}
    holdings = {"data": {"family_name": "Axis ELSS Tax Saver Fund", "month": "2026-08",
                         "equity_holdings": [{"stock_name": "HDFC Bank Ltd", "isin": "INE040A01034", "sector": "Banks", "weight_pct": 9.5},
                                             {"stock_name": "Infosys Ltd", "isin": "INE009A01021", "sector": "IT", "weight_pct": "7.25%"}],
                         "debt_holdings": [{"name": "GOI 2033", "isin": "IN0020230085", "weight_pct": 2.0}],
                         "other_holdings": [{"name": "TREPS", "weight_pct": 1.25}]}}
    web = fake_web({"https://api.example/v1/schemes/120503": json.dumps(scheme).encode(),
                    "https://api.example/v1/families/42/holdings": json.dumps(holdings).encode()})
    r = asyncio.run(fetch_scheme("120503", "Axis ELSS Tax Saver Fund", web, base="https://api.example/v1"))
    assert r["ok"], r["error"]
    s = r["scheme"]
    assert s["scheme"] == "Axis ELSS Tax Saver Fund" and s["as_of"] == date(2026, 8, 31)
    assert [(h["name"], h["kind"], h["weight"]) for h in s["holdings"]][:3] == [
        ("HDFC Bank Ltd", "equity", 9.5), ("Infosys Ltd", "equity", 7.25), ("GOI 2033", "debt", 2.0)]
    assert s["equity_pct"] == 16.75 and s["total_pct"] == 20.0


def test_holdings_api_failure_names_what_it_saw():
    from market_svc.fund_api import fetch_scheme

    web = fake_web({"https://api.example/v1/schemes/1": b'{"data": {"code": "1", "plan": "direct"}}'})
    r = asyncio.run(fetch_scheme("1", "X", web, base="https://api.example/v1"))
    assert not r["ok"] and "code, plan" in r["error"]
    r = asyncio.run(fetch_scheme("2", "X", web, base="https://api.example/v1"))
    assert not r["ok"] and "HTTP 404" in r["error"]


def test_liquid_gold_and_debt_funds_have_nothing_to_look_through():
    from market_svc.fund_holdings import holds_no_stocks

    for name in ("Nippon India ETF Nifty 1D Rate Liquid BeES", "Nippon India ETF Gold BeES", "Zerodha Gold ETF",
                 "HSBC Liquid Fund - Direct Growth", "SBI Magnum Gilt Fund", "Bandhan Banking & PSU Debt Fund",
                 "Bharat Bond ETF April 2030", "ICICI Prudential Silver ETF"):
        assert holds_no_stocks(name), name
    for name in ("Nippon India ETF Nifty 50 BeES", "ICICI Prudential Equity & Debt Fund", "Parag Parikh Flexi Cap Fund",
                 "HDFC Balanced Advantage Fund", "Motilal Oswal Nasdaq 100 ETF"):
        assert not holds_no_stocks(name), name
    assert holds_no_stocks("Some Fund", "Open Ended Schemes(Debt Scheme - Liquid Fund)")
    assert not holds_no_stocks("Some Fund", "Open Ended Schemes(Hybrid Scheme - Aggressive Hybrid Fund)")


def test_a_javascript_page_is_opened_in_a_browser_when_the_plain_download_finds_nothing():
    js_shell = b"<html><div id=root></div><script src=app.js></script></html>"
    web = fake_web({"https://hdfc.example/monthly-portfolio": js_shell,
                    "https://hdfc.example/docs/Monthly-Portfolio-July-2026.xlsx": fp.AMC_A})

    async def browser(url):
        assert url == "https://hdfc.example/monthly-portfolio"
        return 200, '{"items":[{"title":"Monthly Portfolio July 2026","path":"/docs/Monthly-Portfolio-July-2026.xlsx"}]}', url

    targets = {k: v for k, v in fp.TARGETS.items() if v["amc"].startswith("HDFC")}
    r = asyncio.run(fetch_amc("HDFC Mutual Fund", targets, web, today=date(2026, 8, 20), sources=SOURCES, render=browser))
    assert r["ok"] and r["url"].endswith("July-2026.xlsx")
    assert [t["step"] for t in r["tried"]][:2] == ["page", "page (browser)"]


def test_one_file_per_scheme_newest_month_all_tried_initials_first_and_matches_merged():
    # Mirae: sml250_aug2026.xlsx, macif_aug2026.xlsx, … one scheme per file, named with the scheme's initials
    page = "".join(f'<a href="/docs/portfolios/{c}_aug2026.xlsx">{c}</a>' for c in ("sml250", "macif", "manbt", "mamcf", "zzzz", "hfcf"))
    one = fp.AMC_A  # any one-scheme-per-sheet workbook will do: we look at which files are fetched
    fetched = []

    async def web(url):
        fetched.append(url)
        if url == "https://m.example/portfolios":
            return 200, page.encode(), url, "text/html"
        if url.endswith(("hfcf_aug2026.xlsx", "mamcf_aug2026.xlsx")):
            return 200, one, url, "application/octet-stream"
        return 404, b"", url, "text/html"

    targets = {"118955": {"name": "HDFC Flexi Cap Fund", "amc": "HDFC Mutual Fund"}}
    src = {"amcs": [{"amc": "HDFC Mutual Fund", "match": ["hdfc"], "pages": ["https://m.example/portfolios"]}]}
    r = asyncio.run(fetch_amc("HDFC Mutual Fund", targets, web, today=date(2026, 9, 20), sources=src))
    assert fetched[1].endswith("hfcf_aug2026.xlsx")  # the file named like the fund is tried first
    assert r["ok"] and set(r["matched"]) == {"118955"}


def test_a_title_with_its_date_and_a_coded_sheet_still_match():
    from market_svc.fund_portfolio import acronyms, clean_scheme_title, code_of, match_schemes

    assert clean_scheme_title("Portfolio Statement of SBI Small Cap Fund as on 31/08/2026") == "SBI Small Cap Fund"
    assert "mamcf" in acronyms("Mirae Asset Midcap Fund", "Mirae Asset Mutual Fund")
    assert code_of("https://x/portfolios/MAMCF_Aug2026.xlsx?v=2") == "mamcf" and code_of("sml250_aug2026.xlsx") == "sml250"
    h = [{"isin": "INE040A01034", "name": "HDFC Bank", "kind": "equity", "weight": 5.0}]
    schemes = [{"sheet": s, "scheme": s, "titles": [], "holdings": h} for s in ("SCF", "SSCF", "SBLUECHIP")]
    m = match_schemes(schemes, {"1": {"name": "SBI SMALL CAP FUND", "amc": "SBI Mutual Fund"},
                                "2": {"name": "SBI Contra Fund", "amc": "SBI Mutual Fund"}})
    assert m["1"]["index"] == 1 and "2" not in m  # 'SCF' is too short to trust


def test_download_links_without_a_file_extension_are_tried_when_labelled_as_a_monthly_portfolio():
    from market_svc.fund_sources import portfolio_anchors, rank_links_loose

    page = ('<a href="/api/download?id=91">Monthly Portfolio - August 2026</a><a href="/api/download?id=90">Monthly Portfolio - July 2026</a>'
            '<a href="/about.html">Our portfolio of products</a><a href="/api/download?id=7">Fortnightly portfolio Aug 2026</a>')
    ranked = rank_links_loose(portfolio_anchors(page, "https://amc.example/disclosures"))
    assert [u for u, _, _ in ranked] == ["https://amc.example/api/download?id=91", "https://amc.example/api/download?id=90"]
