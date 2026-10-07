"""Request context middleware: correlation ids + structured access log.

Binds ``trace_id``, ``request_id``, ``client_ip`` (and later ``user_id``/``household_id`` from the
auth dependency) into contextvars, so every log line and every audit row for this request
carries them without any explicit plumbing."""
from __future__ import annotations

import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fm_common import http as fm_http
from fm_common.db.session import audit_ctx
from fm_common.logging import bind_context, clear_context, get_logger
from fm_common.loopwatch import watch
from fm_common.ratelimit.middleware import client_ip

log = get_logger("http")
SLOW_MS = 1500  # requests slower than this are logged with what they waited on


def _downstream(rec: list[tuple[str, str, float, int]]) -> dict[str, tuple[float, int]]:
    """Time waited per downstream service (sum of its calls) and the number of calls."""
    out: dict[str, tuple[float, int]] = {}
    for svc, _path, ms, _st in rec:
        t, n = out.get(svc, (0.0, 0))
        out[svc] = (t + ms, n + 1)
    return out


def _current_trace_id() -> str | None:
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        return format(ctx.trace_id, "032x") if ctx.is_valid else None
    except Exception:  # pragma: no cover
        return None


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        headers = dict(scope.get("headers", []))
        request_id = headers.get(b"x-request-id", b"").decode() or uuid.uuid4().hex
        trace_id = _current_trace_id() or request_id
        ip = client_ip(scope)

        clear_context()
        bind_context(request_id=request_id, trace_id=trace_id, client_ip=ip)
        token = audit_ctx.set({"trace_id": trace_id, "client_ip": ip if ip != "unknown" else None})

        start = time.perf_counter()
        status = 500
        rec: list[tuple[str, str, float, int]] = []
        calls_token = fm_http.calls.set(rec)
        timed = scope["type"] == "http" and not scope["path"].startswith("/health")
        if timed:
            watch.request_started(id(scope), scope.get("method", ""), scope["path"])

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                elapsed = (time.perf_counter() - start) * 1000
                message.setdefault("headers", [])
                timing = [f"app;dur={elapsed:.2f}"] + [f'{svc};dur={ms:.2f};desc="{n} call{"s" if n > 1 else ""}"'
                                                       for svc, (ms, n) in _downstream(rec).items()]
                message["headers"] = list(message["headers"]) + [
                    (b"x-request-id", request_id.encode()),
                    (b"server-timing", ", ".join(timing).encode()),
                    (b"access-control-expose-headers", b"server-timing"),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if timed:
                total = round((time.perf_counter() - start) * 1000, 2)
                watch.request_done(id(scope), total)
                log.info("http.request", method=scope.get("method"), path=scope["path"], status=status, duration_ms=total)
                if total >= SLOW_MS:  # what it waited on, slowest first — the start of every performance fix
                    slow = sorted(rec, key=lambda c: -c[2])[:5]
                    log.warning("http.slow_request", method=scope.get("method"), path=scope["path"], status=status, duration_ms=total,
                                waited_on=[f"{svc} {path} {ms:.0f}ms ({st})" for svc, path, ms, st in slow],
                                own_ms=max(0, round(total - sum(ms for _s, _p, ms, _t in rec))))
            fm_http.calls.reset(calls_token)
            audit_ctx.reset(token)
            clear_context()
