"""The Celery application: ``celery -A src.celery_app worker -B`` (``make celery``).

Celery executes; it doesn't orchestrate (CLAUDE.md §7). Every task here is one
retryable unit of work with explicit time limits, late acks and bounded, jittered
retries, on a queue per concern:

- ``notifications`` — delivery attempts, the high-volume queue
- ``maintenance``   — the periodic sweep that re-enqueues lost deliveries

Tasks are async service code run on one event loop per worker *process*: the
database engine is created inside each forked child (an asyncpg connection can't cross a
fork or an event loop), lazily, on first use.

Metrics: the prefork children increment counters that only a multiprocess registry can
collect. When ``PROMETHEUS_MULTIPROC_DIR`` is set (``make celery`` and compose set it),
the parent process serves the aggregate on ``celery.metrics_port``.
"""

import asyncio
import os
from collections.abc import Coroutine
from typing import Any

from celery import Celery
from celery.signals import worker_init, worker_process_init, worker_process_shutdown
from kombu import Queue  # type: ignore[attr-defined]  # re-exported, but kombu has no __all__
from opentelemetry.instrumentation.celery import CeleryInstrumentor
from prometheus_client import CollectorRegistry, multiprocess, start_http_server

import src.modules as modules_package
from platform_core.logging import configure_logging
from platform_core.routing import discover_attribute
from platform_core.telemetry import configure_tracing
from src.config import get_settings
from src.db import postgres

NOTIFICATIONS_QUEUE = "notifications"
MAINTENANCE_QUEUE = "maintenance"

settings = get_settings()

celery_app = Celery(settings.app.service_name, broker=settings.celery.broker_url)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    # Ack after the task finishes, and re-queue a task whose worker died mid-run:
    # at-least-once, with the task itself idempotent.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # One task in hand per child at a time, so a slow delivery doesn't strand others.
    worker_prefetch_multiplier=1,
    task_queues=[Queue(NOTIFICATIONS_QUEUE), Queue(MAINTENANCE_QUEUE)],
    task_default_queue=NOTIFICATIONS_QUEUE,
    broker_connection_retry_on_startup=True,
    # No result backend: outcomes live on the notification row, where the API reads them.
    task_ignore_result=True,
    worker_hijack_root_logger=False,
)

_loop: asyncio.Runner | None = None
_loop_pid: int | None = None


def run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run async service code on this process's own loop, creating it (and the database
    engine bound to it) on first use after a fork."""
    global _loop, _loop_pid
    if _loop is None or _loop_pid != os.getpid():
        _loop = asyncio.Runner()
        _loop_pid = os.getpid()
        postgres.init_postgres(settings.postgres)
    return _loop.run(coro)


def _configure_process() -> None:
    service_name = f"{settings.app.service_name}-celery"
    configure_logging(
        service_name=service_name, level=settings.log.level, json_logs=settings.log.json_logs
    )
    configure_tracing(service_name=service_name, settings=settings.telemetry)
    # Continues the trace carried in the task headers by the enqueuing process.
    CeleryInstrumentor().instrument()


@worker_init.connect
def _on_worker_init(**_: Any) -> None:
    _configure_process()
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR") and settings.celery.metrics_port:
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        start_http_server(settings.celery.metrics_port, registry=registry)


@worker_process_init.connect
def _on_child_init(**_: Any) -> None:
    # Spans and log configuration don't survive the fork reliably; set them up again.
    _configure_process()


@worker_process_shutdown.connect
def _on_child_shutdown(pid: int | None = None, **_: Any) -> None:
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR") and pid is not None:
        multiprocess.mark_process_dead(pid)


# Import every modules/*/tasks.py so its tasks and beat entries register — discovery
# like routes. Each tasks module registers its own beat entries when it is imported, so
# the result doesn't depend on which of the two modules a process imports first (the
# consumer process imports tasks → celery_app; the Celery worker the other way round).
discover_attribute(modules_package, "tasks", "BEAT_SCHEDULE")
