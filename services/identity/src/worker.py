"""The identity worker process: ``python -m src.worker`` (``make worker s=identity``).

Runs the outbox relay, delivering ``user.registered.v1`` to Kafka. Separate from the
API process so a Kafka outage delays the announcement of a new account — it never fails
a registration.
"""

import asyncio

import structlog

from platform_core.kafka import KafkaPublisher, ensure_topics
from platform_core.outbox import OutboxRelay
from platform_core.telemetry import shutdown_tracing
from platform_core.worker import setup_worker_process
from smartcourse_contracts.user_events import USER_REGISTERED_TOPIC
from src.config import get_settings
from src.db import postgres
from src.db.outbox import OutboxMessage

logger = structlog.get_logger(__name__)


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
    await ensure_topics(settings.kafka, [USER_REGISTERED_TOPIC])
    publisher = KafkaPublisher(settings.kafka, client_id=service_name)
    await publisher.start()
    relay = OutboxRelay(
        database=postgres.database,
        model=OutboxMessage,
        deliver=publisher.deliver_outbox,
        settings=settings.outbox,
    )
    try:
        await relay.run_forever()
    finally:
        await publisher.stop()
        await postgres.close_postgres()
        shutdown_tracing()


if __name__ == "__main__":
    asyncio.run(main())
