"""What every worker process (relay, consumers, Temporal workers) sets up before its
loops start: structured logs, tracing, and a /metrics endpoint of its own — a worker
has no HTTP server, and a process nobody can observe is the one that fails silently."""

from platform_core.config import LogSettings, MetricsSettings, TelemetrySettings
from platform_core.logging import configure_logging
from platform_core.metrics import serve_worker_metrics
from platform_core.telemetry import configure_tracing


def setup_worker_process(
    *,
    service_name: str,
    log: LogSettings,
    telemetry: TelemetrySettings,
    metrics: MetricsSettings,
) -> None:
    configure_logging(service_name=service_name, level=log.level, json_logs=log.json_logs)
    configure_tracing(service_name=service_name, settings=telemetry)
    serve_worker_metrics(metrics.worker_port)
