"""advisor-svc — the AI Advisor core: E17–E26, E36-aware rules, E37 Shadow, E38 Scorecard, E39 Sizing, E42 Priority."""
from fm_common.app import create_app

from . import queue
from .api import router
from .consumers import consumer


async def _start() -> None:
    consumer.start()


app = create_app("advisor", [router], on_startup=[_start], on_shutdown=[consumer.stop, queue.close])
