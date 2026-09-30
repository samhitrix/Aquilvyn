"""Yahoo Finance provider (NSE ``.NS`` / BSE ``.BO`` / indices ``^NSEI``).

yfinance is synchronous, so every call runs on a worker thread with bounded concurrency.
Quotes are delayed (typically a few seconds to 15 min) — the Data Quality Engine records the
provider timestamp so reports can say exactly how fresh a number was."""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, date, datetime
from typing import Any

import httpx

from fm_common.logging import get_logger
from fm_common.redact import redact

from .. import health
from .base import Bar, Quote

log = get_logger(__name__)
_sem = asyncio.Semaphore(8)
_http_sem = asyncio.Semaphore(12)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
SOURCE_TIMEOUT = 8.0  # seconds per fundamentals source
CHAIN_BUDGET = 25.0  # seconds for the whole fundamentals chain (callers wait up to 45 s)
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"


class SymbolNotFound(Exception):
    pass


def _pct(v: Any) -> float | None:
    return round(float(v) * 100, 2) if isinstance(v, int | float) else None


def _num(v: Any) -> float | None:
    return round(float(v), 4) if isinstance(v, int | float) else None


class YahooProvider:
    name = "yahoo"

    async def _run(self, fn, *args):
        async with _sem:
            return await asyncio.to_thread(fn, *args)

    _client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=8, headers={"User-Agent": UA, "Accept": "application/json"}, follow_redirects=True)
        return self._client

    async def _chart_quote(self, sym: str) -> Quote:
        """Yahoo's chart endpoint: one small JSON per symbol, no crumb/cookie needed — much faster
        and less rate-limited than yfinance's fast_info (which makes several calls per symbol)."""
        async with _http_sem:
            r = await self._http().get(CHART_URL.format(sym=sym), params={"range": "5d", "interval": "1d"})
        if r.status_code == 429:
            raise RuntimeError("Yahoo rate limit (HTTP 429)")
        if r.status_code == 404:
            raise SymbolNotFound(sym)
        r.raise_for_status()
        res = (r.json().get("chart") or {}).get("result") or []
        if not res:
            raise SymbolNotFound(sym)
        m = res[0]["meta"]
        price = m.get("regularMarketPrice")
        if price is None:
            raise RuntimeError("no price in response")
        ts = datetime.fromtimestamp(m["regularMarketTime"], UTC) if m.get("regularMarketTime") else datetime.now(UTC)
        # previous close = the last daily bar *before* the session of regularMarketTime
        # (chartPreviousClose is the close before the whole 5-day window, so not usable here)
        stamps = res[0].get("timestamp") or []
        closes = ((res[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        offset = int(m.get("gmtoffset") or 19800)
        session = datetime.fromtimestamp(ts.timestamp() + offset, UTC).date()
        before = [c for t, c in zip(stamps, closes, strict=False) if c is not None and datetime.fromtimestamp(t + offset, UTC).date() < session]
        prev = before[-1] if before else (m.get("previousClose") or m.get("chartPreviousClose") or price)
        return Quote(sym, round(float(price), 2), round(float(prev), 2), _num(m.get("regularMarketDayHigh")), _num(m.get("regularMarketDayLow")),
                     int(m.get("regularMarketVolume") or 0), str(m.get("currency") or "INR"), self.name, ts)

    def _yf_quote(self, sym: str) -> Quote | None:
        import yfinance as yf

        fi = yf.Ticker(sym).fast_info
        price = float(fi["last_price"])
        prev = float(fi["previous_close"] or price)
        return Quote(sym, round(price, 2), round(prev, 2), _num(fi.get("day_high")), _num(fi.get("day_low")),
                     int(fi.get("last_volume") or 0), str(fi.get("currency") or "INR"), self.name, datetime.now(UTC))

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        errors: list[str] = []
        not_found: list[str] = []

        async def one(sym: str) -> Quote | None:
            try:
                return await self._chart_quote(sym)
            except SymbolNotFound:
                not_found.append(sym)  # renamed / delisted / wrong suffix — a symbol issue, not a source outage
                return None
            except Exception as exc:
                first = str(exc) or type(exc).__name__
            try:  # fallback: yfinance
                return await self._run(self._yf_quote, sym)
            except Exception as exc:
                errors.append(f"{sym}: {first}; yfinance: {str(exc)[:120] or type(exc).__name__}")
                log.warning("yahoo.quote_failed", symbol=sym, error=first)
                return None

        results = await asyncio.gather(*(one(s) for s in symbols))
        out = {q.symbol: q for q in results if q is not None}
        note = f"Not listed on Yahoo (renamed or delisted?): {', '.join(sorted(not_found)[:8])}" if not_found else ""
        if out:
            await health.record("yahoo", ok=True, count=len(out), note=note)
        elif note:
            await health.record("yahoo", ok=None, note=note)
        if errors:
            await health.record("yahoo", ok=False, error=f"{len(errors)}/{len(symbols)} failed — e.g. {errors[0]}")
        return out

    async def _chart_history(self, sym: str, period: str) -> list[Bar]:
        """Daily bars from the same chart endpoint the quotes use (works where yfinance's history
        silently returns nothing — that left stocks with 0 bars and no 200-DMA)."""
        async with _http_sem:
            r = await self._http().get(CHART_URL.format(sym=sym), params={"range": period, "interval": "1d", "events": "div,splits"})
        if r.status_code == 429:
            raise RuntimeError("Yahoo rate limit (HTTP 429)")
        if r.status_code == 404:
            raise SymbolNotFound(sym)
        r.raise_for_status()
        res = (r.json().get("chart") or {}).get("result") or []
        if not res:
            raise SymbolNotFound(sym)
        offset = int(res[0].get("meta", {}).get("gmtoffset") or 19800)
        q = ((res[0].get("indicators") or {}).get("quote") or [{}])[0]
        out: dict[date, Bar] = {}
        for i, t in enumerate(res[0].get("timestamp") or []):
            c = (q.get("close") or [None])[i] if i < len(q.get("close") or []) else None
            if c is None:
                continue
            def v(k: str, i: int = i, c: float = c) -> float:
                arr = q.get(k) or []
                return round(float(arr[i] if i < len(arr) and arr[i] is not None else c), 2)
            d = datetime.fromtimestamp(t + offset, UTC).date()
            vols = q.get("volume") or []
            out[d] = Bar(d, v("open"), v("high"), v("low"), round(float(c), 2), int((vols[i] if i < len(vols) else 0) or 0))
        return [out[d] for d in sorted(out)]

    async def history(self, symbol: str, days: int = 365) -> list[Bar]:
        import yfinance as yf

        period = "5y" if days > 730 else "2y" if days > 365 else "1y" if days > 180 else "6mo" if days > 90 else "3mo"
        try:
            bars = await self._chart_history(symbol, period)
            if bars:
                return bars[-days:] if days < len(bars) else bars
        except SymbolNotFound:
            raise
        except Exception as exc:
            log.warning("yahoo.chart_history_failed", symbol=symbol, error=str(exc)[:200])

        def load() -> list[Bar]:
            df = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=False)
            return [
                Bar(idx.date(), round(float(r.Open), 2), round(float(r.High), 2), round(float(r.Low), 2),
                    round(float(r.Close), 2), int(r.Volume or 0))
                for idx, r in df.iterrows()
            ]

        bars = await self._run(load)
        return bars[-days:] if days < len(bars) else bars

    async def _yf_fundamentals(self, symbol: str) -> dict[str, Any] | None:
        import yfinance as yf

        def load() -> dict[str, Any] | None:
            info = yf.Ticker(symbol).info or {}
            if not info.get("quoteType"):
                return None
            return {
                "name": info.get("longName") or info.get("shortName"),
                "sector": info.get("sector"), "industry": info.get("industry"),
                "market_cap": info.get("marketCap"), "pe": _num(info.get("trailingPE")),
                "pb": _num(info.get("priceToBook")), "peg": _num(info.get("trailingPegRatio")),
                "eps": _num(info.get("trailingEps")), "roe": _pct(info.get("returnOnEquity")), "roce": None,
                # Yahoo reports D/E as a percentage (e.g. 35.2 => 0.352)
                "debt_to_equity": round(info["debtToEquity"] / 100, 3) if isinstance(info.get("debtToEquity"), int | float) else None,
                "current_ratio": _num(info.get("currentRatio")), "interest_coverage": None,
                "profit_margin": _pct(info.get("profitMargins")), "operating_margin": _pct(info.get("operatingMargins")),
                "revenue_growth": _pct(info.get("revenueGrowth")), "earnings_growth": _pct(info.get("earningsGrowth")),
                "revenue_cagr_3y": None, "eps_cagr_3y": None,
                "dividend_yield": _num(info.get("dividendYield")), "payout_ratio": _pct(info.get("payoutRatio")),
                "beta": _num(info.get("beta")), "promoter_holding": _pct(info.get("heldPercentInsiders")),
                "promoter_pledge": None, "institutional_holding": _pct(info.get("heldPercentInstitutions")),
                "pe_5y_median": None, "sector_pe": None,
                "fifty_two_week_high": _num(info.get("fiftyTwoWeekHigh")), "fifty_two_week_low": _num(info.get("fiftyTwoWeekLow")),
                "book_value": _num(info.get("bookValue")),
            }

        return await self._run(load)

    _crumb: tuple[float, str] | None = None

    async def _summary_fundamentals(self, symbol: str) -> dict[str, Any] | None:
        """Yahoo's quoteSummary API called directly (cookie + crumb) through a Chrome-like session — Yahoo answers
        "429 Too Many Requests" to plain Python HTTP clients, which is why fundamentals never loaded before."""
        from . import browser

        if not self._crumb or time.monotonic() - self._crumb[0] > 3600:
            await browser.get("yahoo", "https://fc.yahoo.com/")  # sets the A3 cookie (the page itself is a 404)
            r = await browser.get("yahoo", "https://query1.finance.yahoo.com/v1/test/getcrumb")
            r.raise_for_status()
            crumb = r.text.strip()
            if not crumb or "<" in crumb or " " in crumb:
                browser.reset("yahoo")
                raise RuntimeError(f"Yahoo gave no crumb ({crumb[:60] or 'empty'})")
            type(self)._crumb = (time.monotonic(), crumb)
        mods = "price,summaryDetail,defaultKeyStatistics,financialData,assetProfile"
        r = await browser.get("yahoo", f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}",
                              params={"modules": mods, "crumb": self._crumb[1]})
        if r.status_code in (401, 403):
            type(self)._crumb = None
            browser.reset("yahoo")
        r.raise_for_status()
        res = ((r.json().get("quoteSummary") or {}).get("result") or [None])[0]
        if not res:
            return None

        def raw(mod: str, key: str) -> Any:
            v = (res.get(mod) or {}).get(key)
            return v.get("raw") if isinstance(v, dict) else v
        de = raw("financialData", "debtToEquity")
        return {
            "name": raw("price", "longName") or raw("price", "shortName"),
            "sector": raw("assetProfile", "sector"), "industry": raw("assetProfile", "industry"),
            "market_cap": raw("price", "marketCap"), "pe": _num(raw("summaryDetail", "trailingPE")),
            "pb": _num(raw("defaultKeyStatistics", "priceToBook")), "peg": _num(raw("defaultKeyStatistics", "pegRatio")),
            "eps": _num(raw("defaultKeyStatistics", "trailingEps")), "roe": _pct(raw("financialData", "returnOnEquity")),
            "debt_to_equity": round(de / 100, 3) if isinstance(de, int | float) else None,
            "current_ratio": _num(raw("financialData", "currentRatio")),
            "profit_margin": _pct(raw("financialData", "profitMargins")), "operating_margin": _pct(raw("financialData", "operatingMargins")),
            "revenue_growth": _pct(raw("financialData", "revenueGrowth")), "earnings_growth": _pct(raw("financialData", "earningsGrowth")),
            "dividend_yield": _num(raw("summaryDetail", "dividendYield")), "payout_ratio": _pct(raw("summaryDetail", "payoutRatio")),
            "beta": _num(raw("summaryDetail", "beta")), "promoter_holding": _pct(raw("defaultKeyStatistics", "heldPercentInsiders")),
            "institutional_holding": _pct(raw("defaultKeyStatistics", "heldPercentInstitutions")),
            "fifty_two_week_high": _num(raw("summaryDetail", "fiftyTwoWeekHigh")), "fifty_two_week_low": _num(raw("summaryDetail", "fiftyTwoWeekLow")),
            "book_value": _num(raw("defaultKeyStatistics", "bookValue")),
        }

    def fundamentals_sources(self, symbol: str) -> list[tuple[str, Any]]:
        from ..config import settings
        from . import keyed_fundamentals, nse_quote, screener

        # fastest, most complete first; yfinance (slow, thread-bound) last
        indian = symbol.upper().endswith((".NS", ".BO"))
        return [
            ("yahoo-api", self._summary_fundamentals),
            ("screener", screener.fundamentals if indian else None),
            ("nse", nse_quote.fundamentals if indian else None),
            ("finnhub", (lambda s: keyed_fundamentals.finnhub(s, settings.finnhub_api_key or "")) if settings.finnhub_api_key else None),
            ("alphavantage", (lambda s: keyed_fundamentals.alphavantage(s, settings.alphavantage_api_key or ""))
             if settings.alphavantage_api_key else None),
            ("yfinance", self._yf_fundamentals),
        ]

    async def fundamentals(self, symbol: str) -> dict[str, Any] | None:
        """Several sources, merged: Yahoo API → Screener.in → NSE → Finnhub / Alpha Vantage → yfinance. Later sources only fill
        what's missing; each gets SOURCE_TIMEOUT seconds so one slow source can't make the whole analysis time out.
        Every failure is logged and recorded (Settings → Data sources) so a gap is never silent."""
        merged: dict[str, Any] = {}
        used: list[str] = []
        errors: list[str] = []
        t0 = time.monotonic()
        for name, fn in self.fundamentals_sources(symbol):
            if fn is None:
                continue
            left = CHAIN_BUDGET - (time.monotonic() - t0)
            if left < 1:
                errors.append(f"{name}: skipped (time budget used up)")
                continue
            try:
                d = await asyncio.wait_for(fn(symbol), min(SOURCE_TIMEOUT, left))
            except TimeoutError:
                errors.append(f"{name}: no answer within {SOURCE_TIMEOUT:.0f}s")
                continue
            except Exception as exc:
                errors.append(f"{name}: {redact(str(exc))[:160] or type(exc).__name__}")
                continue
            if not d:
                errors.append(f"{name}: no data")
                continue
            added = {k: v for k, v in d.items() if v is not None and merged.get(k) is None}
            if added:
                merged.update(added)
                used.append(name)
            if sum(merged.get(k) is not None for k in ("pe", "pb", "roe", "debt_to_equity", "profit_margin", "revenue_growth", "market_cap")) >= 6:
                break  # complete enough — don't hit more sources
        if errors:
            log.warning("fundamentals.partial_sources", symbol=symbol, used=used, errors=errors)
        if not merged:
            raise RuntimeError("; ".join(errors) or "no source had data")
        merged["_sources"] = used
        return merged

    async def news(self, symbol: str, limit: int = 10) -> list[dict[str, Any]]:
        import yfinance as yf

        def load() -> list[dict[str, Any]]:
            items = yf.Ticker(symbol).news or []
            out = []
            for it in items[:limit]:
                c = it.get("content", it)
                out.append({
                    "title": c.get("title"),
                    "publisher": (c.get("provider") or {}).get("displayName") if isinstance(c.get("provider"), dict) else c.get("publisher"),
                    "link": ((c.get("canonicalUrl") or {}).get("url") if isinstance(c.get("canonicalUrl"), dict) else c.get("link")),
                    "published_at": c.get("pubDate") or c.get("providerPublishTime"),
                })
            return out

        try:
            return await self._run(load)
        except Exception as exc:
            log.warning("yahoo.news_failed", symbol=symbol, error=str(exc))
            return []

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        import yfinance as yf

        def load() -> list[dict[str, Any]]:
            res = yf.Search(query, max_results=limit).quotes
            return [
                {"symbol": q["symbol"], "name": q.get("longname") or q.get("shortname"),
                 "asset_type": {"EQUITY": "stock", "ETF": "etf", "INDEX": "index", "MUTUALFUND": "mutual_fund"}.get(q.get("quoteType"), "stock"),
                 "exchange": q.get("exchange"), "sector": q.get("sector")}
                for q in res if q.get("symbol", "").endswith((".NS", ".BO")) or q.get("symbol", "").startswith("^")
            ]

        try:
            return await self._run(load)
        except Exception as exc:
            log.warning("yahoo.search_failed", query=query, error=str(exc))
            return []

    async def corporate_actions(self, symbol: str) -> list[dict[str, Any]]:
        import yfinance as yf

        def load() -> list[dict[str, Any]]:
            acts = yf.Ticker(symbol).actions
            out: list[dict[str, Any]] = []
            for idx, row in acts.iterrows():
                d = idx.date().isoformat()
                if row.get("Dividends"):
                    out.append({"action_type": "dividend", "ex_date": d, "amount": float(row["Dividends"])})
                if row.get("Stock Splits"):
                    # yfinance folds bonuses into splits: ratio r means 1 old -> r new
                    out.append({"action_type": "split", "ex_date": d, "ratio_from": 1.0, "ratio_to": float(row["Stock Splits"])})
            return out

        try:
            return await self._run(load)
        except Exception as exc:
            log.warning("yahoo.actions_failed", symbol=symbol, error=str(exc))
            return []
