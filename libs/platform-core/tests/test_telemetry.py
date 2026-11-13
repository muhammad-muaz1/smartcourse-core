"""Trace context across the hops that aren't HTTP, trace ids on log lines, and the
bounded metric labels. No I/O."""

import json

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from prometheus_client import REGISTRY

from platform_core.config import CorsSettings
from platform_core.logging import configure_logging
from platform_core.middleware import install_middleware
from platform_core.telemetry import current_trace_ids, extract, inject

# The global provider can be set once per process; a test provider is enough to make
# spans real (recording, with ids) without exporting anything.
trace.set_tracer_provider(TracerProvider())
tracer = trace.get_tracer(__name__)


def test_inject_and_extract_carry_one_trace_across_a_hop() -> None:
    with tracer.start_as_current_span("producer"):
        producer_trace = current_trace_ids()
        headers = inject({"event_id": "e1"})
    assert headers["event_id"] == "e1"
    assert "traceparent" in headers

    with tracer.start_as_current_span("consumer", context=extract(headers)):
        consumer_trace = current_trace_ids()
    assert producer_trace is not None and consumer_trace is not None
    assert consumer_trace[0] == producer_trace[0]  # same trace
    assert consumer_trace[1] != producer_trace[1]  # new span


def test_no_trace_outside_a_span() -> None:
    assert current_trace_ids() is None


def test_log_lines_inside_a_span_carry_its_ids(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(service_name="svc", json_logs=True)
    with tracer.start_as_current_span("work"):
        expected = current_trace_ids()
        structlog.get_logger("t").info("inside")
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert expected is not None
    assert (line["trace_id"], line["span_id"]) == expected
    assert line["service.name"] == "svc"


def test_http_metrics_use_the_route_template_not_the_raw_path() -> None:
    app = FastAPI()

    @app.get("/things/{thing_id}")
    def thing(thing_id: str) -> dict[str, str]:
        return {"id": thing_id}

    install_middleware(app, cors=CorsSettings())
    client = TestClient(app)
    client.get("/things/1")
    client.get("/things/2")
    client.get("/nowhere/3")

    def count(route: str, status: str) -> float:
        value = REGISTRY.get_sample_value(
            "http_requests_total", {"method": "GET", "route": route, "status": status}
        )
        return value or 0.0

    assert count("/things/{thing_id}", "200") >= 2
    assert count("unmatched", "404") >= 1
    assert count("/things/1", "200") == 0
