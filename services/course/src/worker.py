"""The course worker process: ``python -m src.worker`` (``make worker s=course``).

Runs, side by side in one process:

- a Temporal worker on ``course-tq`` with every module's ``WORKFLOWS``/``ACTIVITIES``
  (auto-discovered, like routes) — the publishing saga;
- the outbox relay: ``OUTBOX_DISPATCHERS`` topics to Temporal, everything else to Kafka.

Separate from the API process on purpose: the API never talks to Temporal or Kafka, so
an outage of either slows publishing and event delivery down but never fails a request.
"""

import asyncio
from typing import Any

import structlog
from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

import src.modules as modules_package
from platform_core.kafka import KafkaPublisher, ensure_topics
from platform_core.outbox import OutboxMixin, OutboxRelay
from platform_core.routing import discover_attribute
from platform_core.telemetry import shutdown_tracing
from platform_core.worker import setup_worker_process
from smartcourse_contracts.course_events import COURSE_TOPICS
from smartcourse_contracts.platform_events import WORKFLOW_FAILED_TOPIC
from src.config import get_settings
from src.db import postgres
from src.db.outbox import OutboxMessage
from src.modules.courses.dispatchers import DispatchContext

logger = structlog.get_logger(__name__)

PRODUCED_TOPICS = (*COURSE_TOPICS, WORKFLOW_FAILED_TOPIC)


def _flatten(found: list[Any]) -> list[Any]:
    return [item for group in found for item in group]


def _deliver(context: DispatchContext, publisher: KafkaPublisher) -> Any:
    """Topics a module claims (Temporal starts) go to its dispatcher; every event topic
    goes to Kafka."""
    dispatchers: dict[str, Any] = {}
    for mapping in discover_attribute(modules_package, "dispatchers", "OUTBOX_DISPATCHERS"):
        dispatchers.update(mapping)

    async def deliver(message: OutboxMixin) -> None:
        dispatcher = dispatchers.get(message.topic)
        if dispatcher is not None:
            await dispatcher(context, message)
        else:
            await publisher.deliver_outbox(message)

    return deliver


async def main() -> None:
    settings = get_settings()
    service_name = f"{settings.app.service_name}-worker"
    setup_worker_process(
        service_name=service_name,
        log=settings.log,
        telemetry=settings.telemetry,
        metrics=settings.metrics,
    )
    postgres.init_postgres(settings.postgres)
    client = await Client.connect(
        settings.temporal.target,
        namespace=settings.temporal.namespace,
        data_converter=pydantic_data_converter,
        # Trace context crosses into workflows and activities, so a publish is one trace
        # from the HTTP request to the last activity (CLAUDE.md §11).
        interceptors=[TracingInterceptor()],
    )
    worker = Worker(
        client,
        task_queue=settings.temporal.task_queue,
        workflows=_flatten(discover_attribute(modules_package, "workflows", "WORKFLOWS")),
        activities=_flatten(discover_attribute(modules_package, "activities", "ACTIVITIES")),
    )
    await ensure_topics(settings.kafka, PRODUCED_TOPICS)
    publisher = KafkaPublisher(settings.kafka, client_id=service_name)
    await publisher.start()
    relay = OutboxRelay(
        database=postgres.database,
        model=OutboxMessage,
        deliver=_deliver(
            DispatchContext(client=client, task_queue=settings.temporal.task_queue), publisher
        ),
        settings=settings.outbox,
    )
    await logger.ainfo(
        "worker_started", task_queue=settings.temporal.task_queue, target=settings.temporal.target
    )
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(worker.run())
            group.create_task(relay.run_forever())
    finally:
        await publisher.stop()
        await postgres.close_postgres()
        shutdown_tracing()


if __name__ == "__main__":
    asyncio.run(main())
