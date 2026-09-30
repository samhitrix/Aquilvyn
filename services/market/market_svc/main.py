"""market-svc — E10 Market Data, E11 Real-Time Price Stream, E34 Data Quality, E35 Corporate Actions."""
import asyncio

from fm_common.app import background, create_app

from .api import router, seed_on_startup, ws_router
from .stream import hub, poller

poll_start, poll_stop = background(poller, "price-poller")


async def _seed() -> None:
    asyncio.create_task(seed_on_startup(), name="market-seed")


app = create_app("market", [router, ws_router], on_startup=[_seed, hub.start, poll_start], on_shutdown=[poll_stop, hub.stop])
