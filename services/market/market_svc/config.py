from __future__ import annotations

from functools import lru_cache
from typing import Literal

from fm_common.config import CommonSettings


class MarketSettings(CommonSettings):
    # "simulated" works fully offline; "yahoo" = live NSE/BSE via Yahoo Finance (~delayed).
    market_data_provider: Literal["yahoo", "simulated"] = "simulated"
    market_poll_interval_seconds: float = 5.0
    market_hot_symbols_ttl_seconds: int = 300   # symbols stay in the live set this long after last interest
    mf_nav_api: str = "https://api.mfapi.in/mf"
    quote_cache_ttl_seconds: int = 5
    fundamentals_cache_ttl_seconds: int = 6 * 3600
    finnhub_api_key: str | None = None        # optional extra fundamentals sources (see providers/keyed_fundamentals.py)
    alphavantage_api_key: str | None = None
    history_cache_ttl_seconds: int = 900
    # Data Quality Engine (E34) thresholds
    dq_quote_stale_seconds: int = 900
    dq_fundamentals_stale_days: int = 120
    dq_nav_stale_days: int = 5
    # Market-cap size buckets (₹). SEBI ranks: large = top 100, mid = 101–250, small = rest; these
    # approximate AMFI's latest published cut-offs — update them each January/July.
    large_cap_min_inr: float = 9.0e11   # ≈ ₹90,000 Cr
    mid_cap_min_inr: float = 3.0e11     # ≈ ₹30,000 Cr


@lru_cache
def get_settings() -> MarketSettings:
    return MarketSettings()


settings = get_settings()
