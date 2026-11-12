"""The learning worker process: ``python -m src.worker`` (``make worker s=learning``).

A Temporal worker on ``learning-tq`` running every module's ``ACTIVITIES``
(auto-discovered). Today that is ``apply_course_event``, which keeps ``course_cache``
in step with course. Learning owns no workflows yet; bulk enrollment adds the first.
"""

import asyncio
from typing import Any

import structlog
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

import src.modules as modules_package
from platform_core.logging import configure_logging
from platform_core.routing import discover_attribute
from src.config import get_settings
from src.db import postgres

logger = structlog.get_logger(__name__)


def _flatten(found: list[Any]) -> list[Any]:
    return [item for group in found for item in group]


async def main() -> None:
    settings = get_settings()
    configure_logging(
        service_name=f"{settings.app.service_name}-worker",
        level=settings.log.level,
        json_logs=settings.log.json_logs,
    )
    postgres.init_postgres(settings.postgres)
    client = await Client.connect(
        settings.temporal.target,
        namespace=settings.temporal.namespace,
        data_converter=pydantic_data_converter,
    )
    worker = Worker(
        client,
        task_queue=settings.temporal.task_queue,
        activities=_flatten(discover_attribute(modules_package, "activities", "ACTIVITIES")),
    )
    await logger.ainfo(
        "worker_started", task_queue=settings.temporal.task_queue, target=settings.temporal.target
    )
    try:
        await worker.run()
    finally:
        await postgres.close_postgres()


if __name__ == "__main__":
    asyncio.run(main())
