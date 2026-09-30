"""readiness-svc — the Readiness Engine. Keeps asking, for every holding: are the fundamentals loaded? are the
technicals OK? was the call built with today's data? has an AI reviewed it? — and fixes whatever isn't, by asking
the services that own that data (market, advisor)."""
from fm_common.app import create_app

from . import queue
from .api import router
from .consumers import consumer


async def _start() -> None:
    consumer.start()


app = create_app("readiness", [router], on_startup=[_start], on_shutdown=[consumer.stop, queue.close])
