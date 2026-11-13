"""OpenTelemetry tracing: one provider per process, OTLP/HTTP export to Jaeger, and the
helpers that carry trace context across every hop that isn't HTTP (CLAUDE.md §11).

HTTP hops are instrumented automatically (FastAPI in, httpx out). The hops that break
silently are the asynchronous ones, so context travels explicitly:

    request span ──▶ outbox row ``headers`` (captured in the request's transaction)
                 ──▶ relay "publish" span (parent = the captured context)
                 ──▶ Kafka message headers ──▶ consumer "process" span ──▶ Celery headers

``inject()`` / ``extract()`` are the only two functions that touch W3C ``traceparent``.
"""

from collections.abc import Mapping, MutableMapping

import httpx
from fastapi import FastAPI
from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from sqlalchemy.ext.asyncio import AsyncEngine

from platform_core.config import TelemetrySettings

# Probes and scrapes would otherwise be most of the traces in Jaeger.
_UNTRACED_URLS = "/health,/version,/metrics"

_configured = False


def configure_tracing(*, service_name: str, settings: TelemetrySettings) -> None:
    """Idempotent per process: the global provider can only be set once."""
    global _configured
    if _configured:
        return
    provider = TracerProvider(
        resource=Resource.create({SERVICE_NAME: service_name}),
        sampler=ParentBased(TraceIdRatioBased(settings.sample_ratio)),
    )
    if settings.otlp_endpoint:
        exporter = OTLPSpanExporter(endpoint=f"{settings.otlp_endpoint.rstrip('/')}/v1/traces")
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    RedisInstrumentor().instrument()
    _configured = True


def shutdown_tracing() -> None:
    """Flush buffered spans — a worker that exits without this loses its last batch."""
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.shutdown()


def instrument_fastapi(app: FastAPI) -> None:
    FastAPIInstrumentor.instrument_app(app, excluded_urls=_UNTRACED_URLS)


def instrument_httpx_client(client: httpx.AsyncClient) -> None:
    HTTPXClientInstrumentor.instrument_client(client)


def instrument_engine(engine: AsyncEngine) -> None:
    """The instrumentor is a per-process singleton: only the first engine is traced. A
    service process has exactly one engine, so that is the one; the guard keeps tests
    that re-create engines from logging "already instrumented" on every fixture."""
    instrumentor = SQLAlchemyInstrumentor()
    if not instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.instrument(engine=engine.sync_engine)


def inject(carrier: MutableMapping[str, str] | None = None) -> dict[str, str]:
    """The current trace context as W3C headers (``traceparent``, ``tracestate``)."""
    headers: dict[str, str] = dict(carrier or {})
    propagate.inject(headers)
    return headers


def extract(headers: Mapping[str, str]) -> otel_context.Context:
    return propagate.extract(dict(headers))


def current_trace_ids() -> tuple[str, str] | None:
    """(trace_id, span_id) as hex, or None outside a recorded span."""
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return format(span_context.trace_id, "032x"), format(span_context.span_id, "016x")
