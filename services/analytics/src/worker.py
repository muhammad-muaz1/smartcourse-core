"""The analytics worker: ``python -m src.worker`` (``make worker s=analytics``).

One Kafka consumer (group ``analytics``) over every registered topic, projecting each
event into analytics_db. A new consumer group starts from the earliest offset, so a
fresh (or rebuilt) analytics database is filled from the retained history — the reason
event topics are kept forever (docs/adr/0012).
"""

import asyncio

import structlog

import src.modules as modules_package
from platform_core.kafka import EventConsumer, KafkaPublisher, ensure_topics, merge_handlers
from platform_core.routing import discover_attribute
from platform_core.telemetry import shutdown_tracing
from platform_core.worker import setup_worker_process
from src.config import get_settings
from src.db import postgres
from src.db.inbox import ProcessedEvent

logger = structlog.get_logger(__name__)

CONSUMER_GROUP = "analytics"


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
    await ensure_topics(settings.kafka, list(handlers))
    # Only for parking messages it can't project.
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
    try:
        await consumer.run_forever()
    finally:
        await publisher.stop()
        await postgres.close_postgres()
        shutdown_tracing()


if __name__ == "__main__":
    asyncio.run(main())
