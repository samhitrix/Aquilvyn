"""Distributed Tracing Engine (E33f): OpenTelemetry for HTTP (in+out), SQLAlchemy/asyncpg and Redis."""
from __future__ import annotations

from typing import Any

from fm_common.config import settings
from fm_common.logging import get_logger

log = get_logger(__name__)


def setup_telemetry(app: Any, engine: Any | None = None) -> None:
    if not settings.otel_enabled:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(
            resource=Resource.create({"service.name": settings.service_name, "deployment.environment": settings.environment})
        )
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint, insecure=True)))
        trace.set_tracer_provider(provider)
        FastAPIInstrumentor.instrument_app(app, excluded_urls="health")
        HTTPXClientInstrumentor().instrument()
        RedisInstrumentor().instrument()
        if engine is not None:
            SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
        log.info("otel.enabled", endpoint=settings.otel_exporter_otlp_endpoint)
    except Exception as exc:  # never let tracing take a service down
        log.warning("otel.setup_failed", error=str(exc))
