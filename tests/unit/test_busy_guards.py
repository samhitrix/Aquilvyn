"""Background work must never swamp the services: the live-price poller is quiet outside market hours, backs off when
the source refuses, and never falls back to the heavy yfinance path; every service can say what keeps it busy."""
import asyncio
import time
from datetime import UTC, datetime

import httpx


def test_poller_runs_only_in_market_hours():
    from market_svc.stream import market_open

    assert market_open(datetime(2026, 10, 6, 5, 0, tzinfo=UTC))        # Tue 10:30 IST
    assert not market_open(datetime(2026, 10, 6, 14, 0, tzinfo=UTC))   # Tue 19:30 IST
    assert not market_open(datetime(2026, 10, 4, 5, 0, tzinfo=UTC))    # Sunday


def test_poller_backs_off_when_the_source_refuses_and_recovers():
    from market_svc.stream import MAX_BACKOFF, QUIET_INTERVAL, next_wait

    assert next_wait(5, True, 40, 40, 0) == (5, 0)                      # all good, market open
    assert next_wait(5, False, 40, 40, 0) == (QUIET_INTERVAL, 0)        # closed: one look every 15 min
    wait, b = next_wait(5, True, 40, 3, 0)                              # refused → 1 min, then 2, 4 …
    assert (wait, b) == (60, 60) and next_wait(5, True, 40, 0, b)[0] == 120
    assert next_wait(5, True, 40, 0, MAX_BACKOFF)[0] == MAX_BACKOFF     # capped
    assert next_wait(5, True, 40, 40, 480) == (5, 0)                    # back to normal once it answers
    assert next_wait(5, True, 0, 0, 0) == (5, 0)                        # nothing to poll is not a refusal


def test_polling_never_uses_the_heavy_fallback_and_stops_at_a_rate_limit(monkeypatch):
    from market_svc.providers import quick_quotes, yahoo

    sent: list[str] = []

    def answer(req: httpx.Request) -> httpx.Response:
        sent.append(req.url.path)
        return httpx.Response(429)

    async def no_health(*_a, **_k):
        return None

    monkeypatch.setattr(yahoo.health, "record", no_health)
    p = yahoo.YahooProvider()
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    monkeypatch.setattr(p, "_yf_quote", lambda sym: (_ for _ in ()).throw(AssertionError("yfinance must not run while polling")))
    monkeypatch.setattr(yahoo, "_http_sem", asyncio.Semaphore(1))   # one at a time, so "stop at the first 429" is visible

    async def go():
        with quick_quotes():
            return await p.quotes([f"S{i}.NS" for i in range(20)])

    assert asyncio.run(go()) == {}
    assert len(sent) == 1  # the first "slow down" stops the rest of this poll


def test_route_shapes_hide_ids_and_symbols():
    from fm_common.loopwatch import route_of

    assert route_of("/api/v1/market/history/RELIANCE.NS") == "/api/v1/market/history/{id}"
    assert route_of("/api/v1/advisor/recommendations/6942f13e-7e6e-470e-b354-4ef07901e9ca") == "/api/v1/advisor/recommendations/{id}"
    assert route_of("/api/v1/market/fundamentals/120503") == "/api/v1/market/fundamentals/{id}"
    assert route_of("/api/v1/advisor/fund-plan/tax") == "/api/v1/advisor/fund-plan/tax"


def test_loop_watch_names_the_code_that_blocks_the_event_loop():
    from fm_common import loopwatch

    w = loopwatch.LoopWatch()

    def crunch() -> None:  # CPU work on the event loop, the thing that froze every page
        end = time.perf_counter() + 0.8
        while time.perf_counter() < end:
            pass

    async def go():
        await w.start("test")
        await asyncio.sleep(0.15)
        crunch()
        await asyncio.sleep(0.15)
        w._stop.set()
        for t in w._tasks:
            t.cancel()

    asyncio.run(go())
    snap = w.snapshot()
    assert snap["loop"]["max_lag_ms"] >= 400 and snap["loop"]["blocked_ms"] >= 200
    assert any("crunch" in t["where"] or any("crunch" in i for i in t["inside"]) for t in snap["loop"]["top"])


def test_fund_nav_download_keeps_only_recent_years():
    """mfapi.in returns a fund's whole NAV history; only the recent part is kept (decoded off the event loop)."""
    from market_svc.providers.mfapi import MFAPIProvider

    rows = [{"date": f"{(i % 28) + 1:02d}-01-2020", "nav": str(100 + i)} for i in range(5000)]
    p = MFAPIProvider()
    p._client = httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"meta": {"scheme_name": "Example Fund - Direct Growth"}, "data": rows})))
    data = asyncio.run(p._download("120503"))
    assert len(data["data"]) == MFAPIProvider.MAX_ROWS and data["data"][0]["nav"] == "100"  # newest first, kept
