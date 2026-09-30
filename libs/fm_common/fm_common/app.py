"""Service factory: every FolioSense microservice is built the same way, so the
cross-cutting engines (logging, rate limit, cache coherence, tracing, health) are identical
everywhere and a service file only declares its own routers and background tasks."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from fm_common.cache import cache
from fm_common.config import settings
from fm_common.db.session import engine, ping_db
from fm_common.http import ServiceError, close_client
from fm_common.logging import configure_logging, get_logger
from fm_common.middleware import RequestContextMiddleware
from fm_common.ratelimit import RateLimitMiddleware
from fm_common.redis import close_redis, get_redis
from fm_common.telemetry import setup_telemetry

Hook = Callable[[], Awaitable[None]]


def create_app(
    name: str,
    routers: Sequence[APIRouter],
    *,
    on_startup: Sequence[Hook] = (),
    on_shutdown: Sequence[Hook] = (),
    uses_db: bool = True,
    rate_limit: bool = True,
    extra_ready_checks: dict[str, Hook] | None = None,
) -> FastAPI:
    settings.service_name = name
    configure_logging(settings.log_level, service=name)
    log = get_logger(name)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await cache.start_listener()
        for hook in on_startup:
            await hook()
        log.info("service.started", environment=settings.environment)
        yield
        for hook in on_shutdown:
            await hook()
        await cache.stop_listener()
        await close_client()
        await close_redis()
        if uses_db:
            await engine.dispose()
        log.info("service.stopped")

    app = FastAPI(
        title=f"FolioSense · {name}",
        default_response_class=ORJSONResponse,
        lifespan=lifespan,
        docs_url=f"{settings.api_prefix}/{name}/docs",
        openapi_url=f"{settings.api_prefix}/{name}/openapi.json",
    )
    for r in routers:
        app.include_router(r, prefix=settings.api_prefix)

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok", "service": name}

    @app.get("/health/ready", include_in_schema=False)
    async def ready() -> ORJSONResponse:
        checks: dict[str, Hook] = {"redis": _ping_redis}
        if uses_db:
            checks["postgres"] = _ping_db
        checks.update(extra_ready_checks or {})
        results = await asyncio.gather(*(asyncio.wait_for(fn(), 2) for fn in checks.values()), return_exceptions=True)
        status = {k: ("ok" if not isinstance(v, BaseException) else f"fail: {v!s:.80}") for k, v in zip(checks, results, strict=False)}
        healthy = all(v == "ok" for v in status.values())
        return ORJSONResponse({"service": name, "checks": status}, status_code=200 if healthy else 503)

    @app.exception_handler(ServiceError)
    async def _svc_error(_req: Request, exc: ServiceError) -> ORJSONResponse:
        log.warning("upstream.error", upstream=exc.service, status=exc.status)
        code = exc.status if exc.status in (401, 403, 404, 409, 422) else 502
        return ORJSONResponse({"detail": exc.detail, "upstream": exc.service}, status_code=code)

    @app.exception_handler(Exception)
    async def _unhandled(req: Request, exc: Exception) -> ORJSONResponse:
        # A bare "Internal Server Error" tells nobody anything: log the traceback and return the
        # reason (this is a self-hosted app — the person reading it is the owner).
        log.exception("unhandled.error", path=req.url.path, error=f"{type(exc).__name__}: {exc}")
        return ORJSONResponse({"detail": f"{name} error — {type(exc).__name__}: {str(exc)[:300]}", "service": name}, status_code=500)

    # Order matters: last added = outermost. Context must wrap everything else.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["x-request-id", "x-ratelimit-remaining", "server-timing"],
    )
    if rate_limit:
        app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)
    setup_telemetry(app, engine if uses_db else None)
    return app


async def _ping_db() -> None:
    await ping_db()


async def _ping_redis() -> None:
    await get_redis().ping()


def background(coro_fn: Callable[[], Awaitable[Any]], name: str) -> tuple[Hook, Hook]:
    """Helper: (startup, shutdown) hooks that run ``coro_fn`` as a supervised task."""
    task: dict[str, asyncio.Task[Any]] = {}

    async def start() -> None:
        task["t"] = asyncio.create_task(coro_fn(), name=name)

    async def stop() -> None:
        if t := task.get("t"):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    return start, stop
