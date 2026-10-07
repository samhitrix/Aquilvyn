"""Groww fund pages as the primary look-through source — against a synthetic page in Groww's shape, offline."""
import asyncio
import json
from datetime import date

from market_svc.fund_groww import fetch_scheme, kind_of, parse_page, pick_slug, search_queries

HOLDINGS = [
    {"company_name": "HDFC Bank Ltd", "nature_name": "EQUITY", "sector_name": "Financial", "instrument_name": "Equity",
     "corpus_per": 9.5, "portfolio_date": "2026-08-30T18:30:00.000Z"},
    {"company_name": "Infosys Ltd", "nature_name": "EQUITY", "sector_name": "Technology", "instrument_name": "Equity",
     "corpus_per": 7.25, "portfolio_date": "2026-08-30T18:30:00.000Z"},
    {"company_name": "GOI 7.18% 2033", "nature_name": "DEBT", "sector_name": "Sovereign", "instrument_name": "GOI Securities",
     "corpus_per": 2.0, "portfolio_date": "2026-08-30T18:30:00.000Z"},
    {"company_name": "Repo", "nature_name": "CASH", "sector_name": "Unspecified", "instrument_name": "Repo",
     "corpus_per": 1.25, "portfolio_date": "2026-08-30T18:30:00.000Z"},
]
PAGE = ('<html><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"props": {"pageProps": {"mfServerSideData": {"scheme_name": "Axis Large Cap Fund Direct Growth", "holdings": HOLDINGS}}}})
        + "</script></html>")
SEARCH = {"data": {"content": [
    {"entity_type": "Scheme", "title": "Axis Large & Mid Cap Fund Direct Growth", "search_id": "axis-growth-opportunities-fund-direct-growth", "scheme_code": "143"},
    {"entity_type": "Scheme", "title": "Axis Large Cap Fund Direct Growth", "search_id": "axis-bluechip-fund-direct-growth", "scheme_code": "120465"},
]}}


def test_page_holdings_become_a_scheme_with_isins_where_known():
    s = parse_page(PAGE, {"hdfc bank": "INE040A01034"})
    assert s["as_of"] == date(2026, 8, 31)  # 18:30 UTC = midnight in India
    assert [(h["isin"], h["kind"], h["weight"]) for h in s["holdings"]] == [
        ("INE040A01034", "equity", 9.5), ("NAME:infosys", "equity", 7.25), ("NAME:goi 7 18 2033", "debt", 2.0), ("NAME:repo", "other", 1.25)]
    assert s["equity_pct"] == 16.75 and s["total_pct"] == 20.0
    assert parse_page("<html>no data</html>") is None


def test_search_picks_the_fund_by_amfi_code_even_when_its_page_uses_an_old_name():
    assert pick_slug(SEARCH, "120465", "Axis Large Cap Fund - Direct Plan - Growth") == "axis-bluechip-fund-direct-growth"
    # a regular-plan code isn't in the results: the name decides (both plans hold the same portfolio)
    assert pick_slug(SEARCH, "112277", "Axis Large Cap Fund - Regular Plan - Growth", "Axis Mutual Fund") == "axis-bluechip-fund-direct-growth"
    assert pick_slug(SEARCH, "1", "Quant Small Cap Fund") is None
    assert search_queries("Axis Large Cap Fund - Direct Plan - Growth")[1] == "Axis Large Cap Fund"
    assert kind_of("EQUITY") == "equity" and kind_of("CASH") == "other" and kind_of("DEBT") == "debt"


def test_fetch_scheme_end_to_end_and_an_outage_is_flagged():
    async def web(url):
        if "st_query" in url:
            return 200, json.dumps(SEARCH).encode(), url, "application/json"
        if url.endswith("/axis-bluechip-fund-direct-growth"):
            return 200, PAGE.encode(), url, "text/html"
        return 404, b"", url, "text/html"

    r = asyncio.run(fetch_scheme("120465", "Axis Large Cap Fund", "Axis Mutual Fund", web))
    assert r["ok"] and r["url"].endswith("axis-bluechip-fund-direct-growth") and len(r["scheme"]["holdings"]) == 4

    async def down(url):
        return 503, b"", url, "text/html"

    r = asyncio.run(fetch_scheme("120465", "Axis Large Cap Fund", "Axis Mutual Fund", down))
    assert not r["ok"] and r["unreachable"] and "503" in r["error"]

