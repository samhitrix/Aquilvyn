"""analytics-svc — E12 Technical, E13 Fundamental, E14 MF analytics, E15 Risk, E36 Market Regime.
Stateless compute over market-svc data; results live in the cache engine."""
from fm_common.app import create_app

from .api import router

app = create_app("analytics", [router], uses_db=False)
