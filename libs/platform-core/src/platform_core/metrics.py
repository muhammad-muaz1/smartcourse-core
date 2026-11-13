"""Prometheus metrics every service shares, on ``prometheus-client`` directly (docs/adr/0001
§7). API processes serve them at ``GET /metrics`` (health router); worker processes,
which have no HTTP server, serve them on ``metrics.worker_port``.

Labels are bounded on purpose: ``route`` is the route *template* (``/courses/{course_id}``),
never the raw path, and topics/consumer groups are a fixed set. An unbounded label (a raw
path, a user id) grows the series count until Prometheus falls over.

Metric names carry no service prefix — the scrape job adds ``job``/``instance``, which is
how one Grafana panel shows every service side by side.
"""

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    start_http_server,
)
from starlette.responses import Response

# --- HTTP (RequestContextMiddleware) ---------------------------------------------------

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests handled", ["method", "route", "status"]
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# --- outbox relay --------------------------------------------------------------------

OUTBOX_DELIVERED = Counter(
    "outbox_messages_delivered_total", "Outbox rows delivered by the relay", ["topic"]
)
OUTBOX_FAILURES = Counter(
    "outbox_delivery_failures_total", "Outbox delivery attempts that failed", ["topic"]
)
OUTBOX_BACKLOG = Gauge("outbox_backlog", "Outbox rows not yet delivered")
OUTBOX_OLDEST_AGE = Gauge(
    "outbox_oldest_unpublished_age_seconds",
    "Age of the oldest undelivered outbox row — the event lag every consumer inherits",
)

# --- consumers -----------------------------------------------------------------------

# outcome: applied | duplicate | retried | dead_lettered
EVENTS_CONSUMED = Counter(
    "events_consumed_total",
    "Kafka messages handled by a consumer, by outcome",
    ["consumer_group", "topic", "outcome"],
)
EVENT_HANDLE_DURATION = Histogram(
    "event_handle_duration_seconds",
    "Time to handle one Kafka message, inbox claim to commit",
    ["consumer_group", "topic"],
)

# --- background tasks (Celery) -------------------------------------------------------

# outcome: succeeded | retried | failed
TASKS_COMPLETED = Counter(
    "background_tasks_total", "Background task executions, by outcome", ["task", "outcome"]
)


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def serve_worker_metrics(port: int) -> None:
    """A tiny HTTP server on its own thread, for processes without one. 0 = disabled."""
    if port:
        start_http_server(port)
