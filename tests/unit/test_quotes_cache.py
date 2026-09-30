"""Regression: cache.get returns a truthy MISSING sentinel, not None. Treating it as falsy made every
symbol look like a known miss, so no price was ever fetched (everything showed 'at cost')."""
import pytest

from fm_common.cache.l1 import MISSING


def test_missing_sentinel_is_truthy():
    assert bool(MISSING) is True  # so `if not await cache.get(...)` is always wrong


@pytest.mark.asyncio
async def test_get_quotes_fetches_uncached_symbols(monkeypatch):
    from market_svc import quotes
    from market_svc.providers.base import Quote

    store: dict = {}

    class FakeCache:
        async def get(self, k):
            return store.get(k, MISSING)

        async def set(self, k, v, ttl=None, l1_ttl=None):
            store[k] = v

    class FakeProvider:
        calls = 0

        async def quotes(self, syms):
            FakeProvider.calls += 1
            return {s: Quote(s, 101.0, 100.0, source="test") for s in syms}

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(quotes, "cache", FakeCache())
    monkeypatch.setattr(quotes, "get_provider", lambda: FakeProvider())
    monkeypatch.setattr(quotes, "mark_hot", _noop)
    out = await quotes.get_quotes(["120716", "TCS.NS"], wait=2)
    assert out["120716"]["price"] == 101.0 and out["TCS.NS"]["price"] == 101.0 and FakeProvider.calls == 1
    assert "quote-last:TCS.NS" in store  # last good quote kept for when the source is down
    assert await quotes.store_reference_quotes({"TCS.NS": {"price": 99}}) == 0  # never overrides a real quote
    assert await quotes.store_reference_quotes({"INFY.NS": {"price": 1500}}) == 1


def test_yahoo_history_comes_from_the_chart_api():
    import asyncio

    import httpx

    from market_svc.providers import yahoo

    day = 86400
    t0 = 1758513600  # 2025-09-22 03:45 UTC (market open IST)
    body = {"chart": {"result": [{"meta": {"gmtoffset": 19800}, "timestamp": [t0, t0 + day, t0 + 2 * day],
             "indicators": {"quote": [{"open": [100, None, 102], "high": [105, 104, 106], "low": [99, 98, 101],
                                       "close": [104, None, 105], "volume": [1000, 0, 1200]}]}}]}}
    p = yahoo.YahooProvider()
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body)))
    bars = asyncio.run(p.history("TATAPOWER.NS", 400))
    assert [(b.date.isoformat(), b.close) for b in bars] == [("2025-09-22", 104.0), ("2025-09-24", 105.0)]  # empty day skipped
    assert bars[1].open == 102.0 and bars[1].volume == 1200


def test_fundamentals_fall_back_across_sources_and_merge():
    import asyncio

    from market_svc.providers import yahoo

    p = yahoo.YahooProvider()

    async def yf_broken(sym):
        raise KeyError("currentTradingPeriod")   # the yfinance failure seen in the wild

    async def summary_partial(sym):
        return {"name": "Tata Power", "pe": 31.2, "roe": 11.0, "pb": None}

    async def nse(sym):
        return {"name": "TATA POWER CO LTD", "pe": 30.0, "pb": None, "sector_pe": 25.1, "market_cap": 1_200_000_000_000, "industry": "Power"}

    p._yf_fundamentals, p._summary_fundamentals = yf_broken, summary_partial
    import market_svc.providers.nse_quote as nq
    import market_svc.providers.screener as sc

    async def screener_down(sym):
        raise RuntimeError("HTTP 503")

    orig, orig_sc = nq.fundamentals, sc.fundamentals
    nq.fundamentals, sc.fundamentals = nse, screener_down
    try:
        d = asyncio.run(p.fundamentals("TATAPOWER.NS"))
    finally:
        nq.fundamentals, sc.fundamentals = orig, orig_sc
    assert d["pe"] == 31.2 and d["name"] == "Tata Power"                    # earlier source wins
    assert d["sector_pe"] == 25.1 and d["market_cap"] == 1_200_000_000_000  # gaps filled by NSE
    assert d["_sources"] == ["yahoo-api", "nse"]


def test_nse_quote_mapping(monkeypatch):
    import asyncio

    import orjson

    from market_svc.providers import browser, nse_quote

    body = {"info": {"companyName": "Tata Power"}, "industryInfo": {"sector": "Power", "basicIndustry": "Integrated Power Utilities"},
            "metadata": {"pdSymbolPe": "31.5", "pdSectorPe": "24.9"}, "securityInfo": {"issuedSize": 3195339547},
            "priceInfo": {"lastPrice": 380.5, "weekHighLow": {"max": 494.8, "min": 326.3}}}
    seen: list[str] = []

    async def fake_get(name, url, **kw):
        seen.append(url)
        return browser.Response(200, orjson.dumps(body if "/api/" in url else {}).decode(), url)

    monkeypatch.setattr(browser, "get", fake_get)
    monkeypatch.setattr(nse_quote, "_primed", -1e9)  # long ago, whatever the machine uptime
    d = asyncio.run(nse_quote.fundamentals("TATAPOWER.NS"))
    assert seen[0] == "https://www.nseindia.com/"  # cookies first, then the API
    assert d["pe"] == 31.5 and d["sector_pe"] == 24.9 and d["industry"] == "Integrated Power Utilities"
    assert d["market_cap"] == round(380.5 * 3195339547) and d["fifty_two_week_low"] == 326.3


# a synthetic page with the same structure as screener.in's company page (numbers made up)
SCREENER_PAGE = """<html><h1 class="h2 shrink-text">Example Power Ltd</h1>
<ul id="top-ratios">
  <li class="flex flex-space-between"><span class="name">Market Cap</span>
    <span class="nowrap value">&#8377; <span class="number">1,20,000</span> Cr.</span></li>
  <li><span class="name">Current Price</span><span class="nowrap value">&#8377; <span class="number">400</span></span></li>
  <li><span class="name">High / Low</span><span class="nowrap value">&#8377; <span class="number">490</span> / <span class="number">320</span></span></li>
  <li><span class="name">Stock P/E</span><span class="nowrap value"><span class="number">32.5</span></span></li>
  <li><span class="name">Book Value</span><span class="nowrap value">&#8377; <span class="number">100</span></span></li>
  <li><span class="name">Dividend Yield</span><span class="nowrap value"><span class="number">0.55</span> %</span></li>
  <li><span class="name">ROCE</span><span class="nowrap value"><span class="number">12.1</span> %</span></li>
  <li><span class="name">ROE</span><span class="nowrap value"><span class="number">11.4</span> %</span></li>
</ul>
<table class="ranges-table"><tr><th colspan="2">Compounded Sales Growth</th></tr>
  <tr><td>10 Years:</td><td>7%</td></tr><tr><td>3 Years:</td><td>18%</td></tr><tr><td>TTM:</td><td>-2%</td></tr></table>
<table class="ranges-table"><tr><th colspan="2">Compounded Profit Growth</th></tr>
  <tr><td>3 Years:</td><td>25%</td></tr><tr><td>TTM:</td><td>9%</td></tr></table>
<section id="shareholding"><table class="data-table">
  <tr><th></th><th>Mar 2026</th><th>Jun 2026</th></tr>
  <tr class="stripe"><td class="text"><button class="button-plain">Promoters&nbsp;<span>+</span></button></td><td>46.90%</td><td>46.86%</td></tr>
  <tr><td class="text">FIIs</td><td>9.1%</td><td>9.4%</td></tr>
</table></section></html>"""


def test_screener_page_parsing():
    from market_svc.providers import screener

    d = screener.parse(SCREENER_PAGE)
    assert d["name"] == "Example Power Ltd" and d["market_cap"] == 1_20_000 * 10_000_000
    assert d["pe"] == 32.5 and d["book_value"] == 100 and d["pb"] == 4.0
    assert d["roe"] == 11.4 and d["roce"] == 12.1 and d["dividend_yield"] == 0.0055
    assert (d["fifty_two_week_high"], d["fifty_two_week_low"]) == (490, 320)
    assert (d["revenue_growth"], d["revenue_cagr_3y"], d["earnings_growth"], d["eps_cagr_3y"]) == (-2, 18, 9, 25)
    assert d["promoter_holding"] == 46.86  # latest quarter
    assert screener.parse("<html>login required</html>") is None


def test_screener_falls_back_to_standalone(monkeypatch):
    import asyncio

    from market_svc.providers import browser, screener

    urls: list[str] = []

    async def fake_get(name, url, **kw):
        urls.append(url)
        return browser.Response(200, "<html>no ratios</html>" if "consolidated" in url else SCREENER_PAGE, url)

    monkeypatch.setattr(browser, "get", fake_get)
    d = asyncio.run(screener.fundamentals("EXPOWER.NS"))
    assert urls == ["https://www.screener.in/company/EXPOWER/consolidated/", "https://www.screener.in/company/EXPOWER/"]
    assert d["pe"] == 32.5


def test_yahoo_and_nse_use_a_browser_like_session():
    """Yahoo answers 429 to plain Python TLS — fundamentals only work through curl_cffi's Chrome impersonation."""
    from market_svc.providers import browser

    assert browser.impersonating()


def test_finnhub_and_alphavantage_mapping(monkeypatch):
    import asyncio

    import httpx

    from market_svc.providers import keyed_fundamentals as kf

    fh = {"metric": {"peTTM": 22.5, "pbQuarterly": 3.1, "roeTTM": 18.2, "totalDebt/totalEquityQuarterly": 0.4, "marketCapitalization": 150000,
                     "dividendYieldIndicatedAnnual": 1.5}}
    av = {"Note": "Thank you for using Alpha Vantage! Our standard API rate limit is 25 requests per day."}
    real = httpx.AsyncClient

    def client(**kw):
        return real(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=fh if "finnhub" in r.url.host else av)), **kw)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    d = asyncio.run(kf.finnhub("INFY.NS", "k"))
    assert d["pe"] == 22.5 and d["roe"] == 18.2 and d["market_cap"] == 150_000_000_000 and d["dividend_yield"] == 0.015
    try:
        asyncio.run(kf.alphavantage("INFY.NS", "k"))
        raise AssertionError("rate-limit note must surface as an error, not as 'no data'")
    except RuntimeError as exc:
        assert "25 requests per day" in str(exc)


def test_a_hanging_fundamentals_source_is_skipped_not_waited_for(monkeypatch):
    import asyncio
    import time

    from market_svc.providers import yahoo

    monkeypatch.setattr(yahoo, "SOURCE_TIMEOUT", 0.2)
    p = yahoo.YahooProvider()

    async def hangs(sym):
        await asyncio.sleep(30)

    async def nse(sym):
        return {"pe": 20.0, "market_cap": 1e12}

    p._summary_fundamentals = hangs
    monkeypatch.setattr(p, "fundamentals_sources", lambda s: [("yahoo-api", hangs), ("nse", nse), ("yfinance", hangs)])
    t0 = time.perf_counter()
    d = asyncio.run(p.fundamentals("X.NS"))
    assert d["pe"] == 20.0 and d["_sources"] == ["nse"] and time.perf_counter() - t0 < 2
