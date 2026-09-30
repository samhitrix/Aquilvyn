"""Pure-ASGI middleware (no BaseHTTPMiddleware overhead, works for websockets too)."""
from __future__ import annotations

import math
from dataclasses import dataclass

import orjson
from starlette.types import ASGIApp, Receive, Scope, Send

from fm_common.config import settings
from fm_common.identity.tokens import peek_subject
from fm_common.logging import get_logger
from fm_common.redis import get_redis

from .engine import SlidingWindowLimiter

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Rule:
    prefix: str
    limit: int
    window: int
    scope: str  # "ip" | "user"


def default_rules() -> list[Rule]:
    w = settings.rate_limit_window_seconds
    return [
        # Credential endpoints are always keyed by IP: brute force / credential stuffing.
        Rule(f"{settings.api_prefix}/auth/login", settings.rate_limit_auth, w, "ip"),
        Rule(f"{settings.api_prefix}/auth/register", settings.rate_limit_auth, w, "ip"),
        Rule(f"{settings.api_prefix}/auth/refresh", settings.rate_limit_auth * 3, w, "ip"),
        Rule(settings.api_prefix, settings.rate_limit_default, w, "user"),
    ]


EXEMPT = ("/health", "/metrics")


def client_ip(scope: Scope) -> str:
    # The gateway (nginx) sets X-Real-IP from the TCP peer; never trust arbitrary X-Forwarded-For hops.
    for name, value in scope.get("headers", []):
        if name == b"x-real-ip":
            return value.decode()
    client = scope.get("client")
    return client[0] if client else "unknown"


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp, rules: list[Rule] | None = None) -> None:
        self.app = app
        self.rules = rules or default_rules()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not settings.rate_limit_enabled:
            return await self.app(scope, receive, send)
        path: str = scope["path"]
        if path.startswith(EXEMPT) or scope["method"] == "OPTIONS":
            return await self.app(scope, receive, send)
        rule = next((r for r in self.rules if path.startswith(r.prefix)), None)
        if rule is None:
            return await self.app(scope, receive, send)

        ip = client_ip(scope)
        identity = f"ip:{ip}"
        if rule.scope == "user":
            sub = _bearer_subject(scope)
            if sub == "service":  # a signed internal call (readiness → advisor, analytics → market …): never throttled
                return await self.app(scope, receive, send)
            if sub:
                identity = f"user:{sub}"
        bucket = f"{rule.prefix}|{identity}"

        try:
            result = await SlidingWindowLimiter(get_redis()).hit(bucket, rule.limit, rule.window)
        except Exception as exc:  # fail-open: availability over strictness if Redis blips
            log.warning("ratelimit.redis_unavailable", error=str(exc))
            return await self.app(scope, receive, send)

        headers = [
            (b"x-ratelimit-limit", str(result.limit).encode()),
            (b"x-ratelimit-remaining", str(result.remaining).encode()),
            (b"x-ratelimit-reset", str(math.ceil(result.reset_after_ms / 1000)).encode()),
        ]
        if not result.allowed:
            log.warning("ratelimit.blocked", identity=identity, rule=rule.prefix)
            body = orjson.dumps({"detail": "Too many requests", "retry_after_seconds": math.ceil(result.reset_after_ms / 1000)})
            await send({
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"retry-after", str(math.ceil(result.reset_after_ms / 1000)).encode()),
                    *headers,
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _bearer_subject(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"authorization" and value[:7].lower() == b"bearer ":
            return peek_subject(value[7:].decode())
    return None
