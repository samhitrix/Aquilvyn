"""identity-svc — E02 Unified Identity Engine (+ household members, audit read API)."""
from fm_common.app import create_app

from .api_auth import router as auth_router
from .api_members import router as members_router

app = create_app("identity", [auth_router, members_router])
