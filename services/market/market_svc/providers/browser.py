"""A browser-like HTTP session for sites that refuse plain Python clients.

Since 2025 Yahoo Finance (and NSE's Akamai front) answer "429 Too Many Requests" / 403 to requests whose TLS
handshake doesn't look like a real browser — whatever the User-Agent says. ``curl_cffi`` makes the handshake
look like Chrome's, which is what yfinance itself switched to. If curl_cffi isn't installed we fall back to
httpx, so the service still starts (those sources then fail with a clear error instead).
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

_sessions: dict[tuple[str, int], Any] = {}


def impersonating() -> bool:
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        return False
    return True


class Response:
    """The few bits of a response the providers use — same shape for curl_cffi and httpx."""

    def __init__(self, status_code: int, text: str, url: str) -> None:
        self.status_code, self.text, self.url = status_code, text, url

    def json(self) -> Any:
        import orjson

        return orjson.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            snippet = " ".join(self.text.split())[:160]
            raise httpx.HTTPStatusError(f"HTTP {self.status_code} from {self.url.split('?')[0]}: {snippet}",
                                        request=httpx.Request("GET", self.url), response=httpx.Response(self.status_code, text=self.text))


def _session(name: str, headers: dict[str, str]) -> Any:
    """One cookie-keeping session per site and event loop (curl_cffi sessions are bound to their loop)."""
    key = (name, id(asyncio.get_running_loop()))
    s = _sessions.get(key)
    if s is None:
        if impersonating():
            from curl_cffi.requests import AsyncSession

            s = AsyncSession(impersonate="chrome", headers=headers, timeout=10, allow_redirects=True)
        else:
            s = httpx.AsyncClient(timeout=10, follow_redirects=True, headers={"User-Agent": UA, **headers})
        _sessions[key] = s
    return s


async def get(name: str, url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
              base_headers: dict[str, str] | None = None) -> Response:
    s = _session(name, base_headers or {})
    r = await s.get(url, params=params, headers=headers)
    return Response(r.status_code, r.text, str(r.url))


def reset(name: str) -> None:
    """Forget a site's cookies (e.g. after a 401 / 403) — the next call starts a fresh session."""
    for key in [k for k in _sessions if k[0] == name]:
        _sessions.pop(key, None)


async def get_bytes(name: str, url: str, *, seconds: float = 60, headers: dict[str, str] | None = None) -> tuple[int, bytes, str, str]:
    """A file download (Excel / zip / HTML page as bytes) → (status, body, final url, content-type)."""
    if impersonating():
        from curl_cffi.requests import AsyncSession

        async with AsyncSession(impersonate="chrome", timeout=seconds, allow_redirects=True, headers=headers or {}) as s:
            r = await s.get(url)
            return r.status_code, r.content, str(r.url), r.headers.get("content-type", "")
    async with httpx.AsyncClient(timeout=seconds, follow_redirects=True, headers={"User-Agent": UA, **(headers or {})}) as c:
        r = await c.get(url)
        return r.status_code, r.content, str(r.url), r.headers.get("content-type", "")
