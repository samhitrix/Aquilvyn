"""Request context middleware: correlation ids + structured access log.

Binds ``trace_id``, ``request_id``, ``client_ip`` (and later ``user_id``/``household_id`` from the
auth dependency) into contextvars, so every log line and every audit row for this request
carries them without any explicit plumbing."""
from __future__ import annotations

import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fm_common.db.session import audit_ctx
from fm_common.logging import bind_context, clear_context, get_logger
from fm_common.ratelimit.middleware import client_ip

log = get_logger("http")


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

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                elapsed = (time.perf_counter() - start) * 1000
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + [
                    (b"x-request-id", request_id.encode()),
                    (b"server-timing", f"app;dur={elapsed:.2f}".encode()),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if scope["type"] == "http" and not scope["path"].startswith("/health"):
                log.info(
                    "http.request",
                    method=scope.get("method"),
                    path=scope["path"],
                    status=status,
                    duration_ms=round((time.perf_counter() - start) * 1000, 2),
                )
            audit_ctx.reset(token)
            clear_context()
