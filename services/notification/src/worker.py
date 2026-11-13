"""The notification event worker: ``python -m src.worker`` (``make worker s=notification``).

Runs, side by side in one process:

- the Kafka consumer (group ``notification``): turns ``user.registered.v1``,
  ``enrollment.created.v1`` and ``enrollment.completed.v1`` into notification rows and
  enqueues their Celery delivery after commit;
- the outbox relay, delivering ``workflow.failed.v1`` for deliveries that gave up.

The deliveries themselves run in the Celery worker (``make celery``), a separate process:
a slow mail relay backs up a Celery queue, never the Kafka partition.
"""

import asyncio

import structlog
from opentelemetry.instrumentation.celery import CeleryInstrumentor

import src.modules as modules_package
from platform_core.kafka import EventConsumer, KafkaPublisher, ensure_topics, merge_handlers
from platform_core.outbox import OutboxRelay
from platform_core.routing import discover_attribute
from platform_core.telemetry import shutdown_tracing
from platform_core.worker import setup_worker_process
from smartcourse_contracts.platform_events import WORKFLOW_FAILED_TOPIC
from smartcourse_contracts.topics import DEAD_LETTER_TOPIC
from src.config import get_settings
from src.db import postgres
from src.db.inbox import ProcessedEvent
from src.db.outbox import OutboxMessage

logger = structlog.get_logger(__name__)

CONSUMER_GROUP = "notification"


async def main() -> None:
    settings = get_settings()
    service_name = f"{settings.app.service_name}-worker"
    setup_worker_process(
        service_name=service_name,
        log=settings.log,
        telemetry=settings.telemetry,
        metrics=settings.metrics,
    )
    # Injects the consumer span's context into the task headers on enqueue.
    CeleryInstrumentor().instrument()  # type: ignore[no-untyped-call]  # OTel instrumentors are untyped
    postgres.init_postgres(settings.postgres)
    handlers = merge_handlers(discover_attribute(modules_package, "consumers", "CONSUMERS"))
    await ensure_topics(settings.kafka, [*handlers, WORKFLOW_FAILED_TOPIC, DEAD_LETTER_TOPIC])
    publisher = KafkaPublisher(settings.kafka, client_id=service_name)
    await publisher.start()
    consumer = EventConsumer(
        group_id=CONSUMER_GROUP,
        handlers=handlers,
        database=postgres.database,
        inbox_model=ProcessedEvent,
        settings=settings.kafka,
        publisher=publisher,
    )
    relay = OutboxRelay(
        database=postgres.database,
        model=OutboxMessage,
        deliver=publisher.deliver_outbox,
        settings=settings.outbox,
    )
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(consumer.run_forever())
            group.create_task(relay.run_forever())
    finally:
        await publisher.stop()
        await postgres.close_postgres()
        shutdown_tracing()


if __name__ == "__main__":
    asyncio.run(main())
