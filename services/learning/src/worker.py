"""The learning worker process: ``python -m src.worker`` (``make worker s=learning``).

Runs, side by side in one process:

- the Kafka consumer (group ``learning``): every module's ``CONSUMERS`` — today
  ``course.*.v1`` → ``course_cache`` and ``user.registered.v1`` → ``student_directory``;
- the outbox relay, delivering ``enrollment.*.v1`` to Kafka.

Separate from the API process: enrolling never waits on Kafka, and a Kafka outage delays
the side effects of an enrollment, never the enrollment itself.
"""

import asyncio

import structlog

import src.modules as modules_package
from platform_core.kafka import EventConsumer, KafkaPublisher, ensure_topics, merge_handlers
from platform_core.outbox import OutboxRelay
from platform_core.routing import discover_attribute
from platform_core.telemetry import shutdown_tracing
from platform_core.worker import setup_worker_process
from smartcourse_contracts.enrollment_events import ENROLLMENT_TOPICS
from smartcourse_contracts.topics import DEAD_LETTER_TOPIC
from src.config import get_settings
from src.db import postgres
from src.db.inbox import ProcessedEvent
from src.db.outbox import OutboxMessage

logger = structlog.get_logger(__name__)

CONSUMER_GROUP = "learning"


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
    handlers = merge_handlers(discover_attribute(modules_package, "consumers", "CONSUMERS"))
    await ensure_topics(settings.kafka, [*handlers, *ENROLLMENT_TOPICS, DEAD_LETTER_TOPIC])
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
