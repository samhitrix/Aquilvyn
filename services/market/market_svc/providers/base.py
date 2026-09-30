"""Provider contract. Every data source (Yahoo, mfapi, a paid NSE feed, a broker WebSocket
in P3) implements this; nothing else in market-svc knows where data comes from."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Protocol


@dataclass(slots=True)
class Quote:
    symbol: str
    price: float
    prev_close: float
    day_high: float | None = None
    day_low: float | None = None
    volume: int | None = None
    currency: str = "INR"
    source: str = "unknown"
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def change(self) -> float:
        return self.price - self.prev_close

    @property
    def change_pct(self) -> float:
        return (self.change / self.prev_close * 100) if self.prev_close else 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ts"] = self.ts.isoformat()
        d["change"] = round(self.change, 4)
        d["change_pct"] = round(self.change_pct, 4)
        return d


@dataclass(slots=True)
class Bar:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"date": self.date.isoformat(), "open": self.open, "high": self.high, "low": self.low, "close": self.close, "volume": self.volume}


class MarketDataProvider(Protocol):
    name: str

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]: ...

    async def history(self, symbol: str, days: int = 365) -> list[Bar]: ...

    async def fundamentals(self, symbol: str) -> dict[str, Any] | None: ...

    async def news(self, symbol: str, limit: int = 10) -> list[dict[str, Any]]: ...

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]: ...

    async def corporate_actions(self, symbol: str) -> list[dict[str, Any]]: ...


# Normalised fundamentals keys every provider must map to (None when unknown).
FUNDAMENTAL_KEYS = (
    "market_cap", "pe", "pb", "peg", "eps", "roe", "roce", "debt_to_equity", "current_ratio",
    "interest_coverage", "profit_margin", "operating_margin", "revenue_growth", "earnings_growth",
    "revenue_cagr_3y", "eps_cagr_3y", "dividend_yield", "payout_ratio", "beta", "promoter_holding",
    "promoter_pledge", "institutional_holding", "pe_5y_median", "sector_pe", "fifty_two_week_high",
    "fifty_two_week_low", "book_value",
)
