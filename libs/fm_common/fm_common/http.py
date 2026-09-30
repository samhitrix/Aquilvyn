"""Internal service-to-service HTTP client.

* One pooled ``httpx.AsyncClient`` per process (HTTP keep-alive between services).
* Propagates ``X-Request-ID`` and W3C ``traceparent`` (OTel httpx instrumentation) so a
  single trace spans gateway -> dashboard-bff -> portfolio/market/advisor.
* Forwards the caller's JWT (on-behalf-of) so the downstream service enforces the same
  household/RBAC scope; background jobs use a short-lived service token instead.
"""
from __future__ import annotations

from typing import Any

import httpx
from structlog.contextvars import get_contextvars

from fm_common.config import settings
from fm_common.identity.tokens import create_service_token

_client: httpx.AsyncClient | None = None


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
    h: dict[str, str] = {"Authorization": f"Bearer {token or create_service_token(settings.service_name)}"}
    if rid := get_contextvars().get("request_id"):
        h["X-Request-ID"] = rid
    return h


async def call(
    base_url: str, method: str, path: str, *, token: str | None = None, json: Any = None, params: dict | None = None,
    request_timeout: float | None = None,
) -> Any:
    kw: dict[str, Any] = {"timeout": request_timeout} if request_timeout else {}
    resp = await client().request(method, f"{base_url}{path}", json=json, params=params, headers=_headers(token), **kw)
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
