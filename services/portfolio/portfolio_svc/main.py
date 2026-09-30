"""portfolio-svc — E05 Ledger, E06 Holdings & Valuation, E07 Import, E08 Family & Profiles, E35 (ledger side)."""
from fm_common.app import create_app

from .api import router
from .consumers import consumer
from .tax_api import router as tax_router


async def _start() -> None:
    consumer.start()


app = create_app("portfolio", [router, tax_router], on_startup=[_start], on_shutdown=[consumer.stop])
