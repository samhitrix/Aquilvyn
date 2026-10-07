"""market-svc — E10 Market Data, E11 Real-Time Price Stream, E34 Data Quality, E35 Corporate Actions.

The API process only answers requests (and fans live prices out to browsers). Background work — the price poller and
the history warm-up — runs in the market worker, so a slow or refusing data source never slows a page."""
import asyncio

from fm_common.app import create_app

from .api import router, seed_on_startup, ws_router
from .stream import hub


async def _seed() -> None:
    asyncio.create_task(seed_on_startup(), name="market-seed")


app = create_app("market", [router, ws_router], on_startup=[_seed, hub.start], on_shutdown=[hub.stop])
