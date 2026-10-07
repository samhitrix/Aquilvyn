"""Internal service-to-service HTTP client.

* One pooled ``httpx.AsyncClient`` per process (HTTP keep-alive between services).
* Propagates ``X-Request-ID`` and W3C ``traceparent`` (OTel httpx instrumentation) so a
  single trace spans gateway -> dashboard-bff -> portfolio/market/advisor.
* Forwards the caller's JWT (on-behalf-of) so the downstream service enforces the same
  household/RBAC scope; background jobs use a short-lived service token instead.
"""
from __future__ import annotations

import contextvars
import time
from typing import Any

import httpx
from structlog.contextvars import get_contextvars

from fm_common.config import settings
from fm_common.identity.tokens import INTERNAL_HEADER, create_service_token, internal_proof

_client: httpx.AsyncClient | None = None
# downstream calls made while serving the current request: (service, path, ms, status) — shown in Server-Timing and
# in the slow-request log, so a slow page says *what* it was waiting on
calls: contextvars.ContextVar[list[tuple[str, str, float, int]] | None] = contextvars.ContextVar("fm_downstream_calls", default=None)


def _service_of(base_url: str) -> str:
    """http://market:8000 / http://localhost:8003 → market (by host, else by the dev port)."""
    host = base_url.split("//", 1)[-1].split("/", 1)[0]
    name, _, port = host.partition(":")
    dev = {"8001": "identity", "8002": "portfolio", "8003": "market", "8004": "analytics", "8005": "advisor", "8006": "dashboard", "8007": "readiness"}
    return dev.get(port, name) if name in ("localhost", "127.0.0.1") else name


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=settings.internal_timeout_seconds,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


class ServiceError(Exception):
    def __init__(self, service: str, status: int, detail: Any) -> None:
        super().__init__(f"{service} returned {status}: {detail}")
        self.service, self.status, self.detail = service, status, detail


def _headers(token: str | None) -> dict[str, str]:
    bearer = token or create_service_token(settings.service_name)
    h: dict[str, str] = {"Authorization": f"Bearer {bearer}", INTERNAL_HEADER: internal_proof(bearer)}
    if rid := get_contextvars().get("request_id"):
        h["X-Request-ID"] = rid
    return h


async def call(
    base_url: str, method: str, path: str, *, token: str | None = None, json: Any = None, params: dict | None = None,
    request_timeout: float | None = None,
) -> Any:
    kw: dict[str, Any] = {"timeout": request_timeout} if request_timeout else {}
    start = time.perf_counter()
    status = 0
    try:
        resp = await client().request(method, f"{base_url}{path}", json=json, params=params, headers=_headers(token), **kw)
        status = resp.status_code
    finally:
        rec = calls.get()
        if rec is not None:
            rec.append((_service_of(base_url), path.split("?", 1)[0], (time.perf_counter() - start) * 1000, status))
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text
        raise ServiceError(base_url, resp.status_code, detail)
    return resp.json() if resp.content else None


async def get(base_url: str, path: str, *, token: str | None = None, params: dict | None = None, request_timeout: float | None = None) -> Any:
    return await call(base_url, "GET", path, token=token, params=params, request_timeout=request_timeout)


async def post(base_url: str, path: str, *, token: str | None = None, json: Any = None) -> Any:
    return await call(base_url, "POST", path, token=token, json=json)
