from __future__ import annotations

import atexit
import logging
import logging.handlers
import queue
import sys
from typing import Any

import orjson
import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars, merge_contextvars

from .masking import mask_pii_processor

_listener: logging.handlers.QueueListener | None = None


def _add_otel_ids(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Prefer the active OpenTelemetry trace id so logs and traces join in Grafana."""
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            event_dict.setdefault("trace_id", format(ctx.trace_id, "032x"))
            event_dict.setdefault("span_id", format(ctx.span_id, "016x"))
    except Exception:  # pragma: no cover - otel optional
        pass
    return event_dict


def _orjson_dumps(obj: Any, default: Any = None) -> str:
    return orjson.dumps(obj, default=default or str).decode()


def configure_logging(level: str = "INFO", service: str = "foliosense-api") -> None:
    global _listener

    shared: list[Any] = [
        merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        _add_otel_ids,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        mask_pii_processor,
    ]

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,  # stdlib loggers (uvicorn, sqlalchemy) get the same treatment
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            lambda _l, _m, ed: {"service": service, **ed},
            structlog.processors.JSONRenderer(serializer=_orjson_dumps),
        ],
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)

    # Non-blocking: the event loop only enqueues; a thread does the stdout write.
    q: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    queue_handler = logging.handlers.QueueHandler(q)
    # QueueHandler.prepare() would pre-format with its own formatter; keep the record intact.
    queue_handler.prepare = lambda record: record  # type: ignore[method-assign]

    root = logging.getLogger()
    root.handlers = [queue_handler]
    root.setLevel(level.upper())

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "arq"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    # Access logs are emitted by our own middleware with richer context.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    if _listener is not None:
        _listener.stop()
    _listener = logging.handlers.QueueListener(q, stream, respect_handler_level=False)
    _listener.start()
    atexit.register(_listener.stop)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def bind_context(**kwargs: Any) -> None:
    bind_contextvars(**{k: v for k, v in kwargs.items() if v is not None})


def clear_context() -> None:
    clear_contextvars()
