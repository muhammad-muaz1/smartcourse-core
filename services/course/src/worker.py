"""The course worker process: ``python -m src.worker`` (``make worker s=course``).

Runs, side by side in one process:

- a Temporal worker on ``course-tq`` with every module's ``WORKFLOWS``/``ACTIVITIES``
  (auto-discovered, like routes);
- the outbox relay, delivering every module's ``OUTBOX_DISPATCHERS`` topics.

Separate from the API process on purpose: the API never talks to Temporal, so a
Temporal outage slows publishing down but never fails a request.
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
from src.modules.courses.dispatchers import DispatchContext
from src.relay import Dispatcher, run_relay

logger = structlog.get_logger(__name__)


def _flatten(found: list[Any]) -> list[Any]:
    return [item for group in found for item in group]


def _dispatchers() -> dict[str, Dispatcher]:
    merged: dict[str, Dispatcher] = {}
    for mapping in discover_attribute(modules_package, "dispatchers", "OUTBOX_DISPATCHERS"):
        merged.update(mapping)
    return merged


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
        workflows=_flatten(discover_attribute(modules_package, "workflows", "WORKFLOWS")),
        activities=_flatten(discover_attribute(modules_package, "activities", "ACTIVITIES")),
    )
    context = DispatchContext(client=client, task_queue=settings.temporal.task_queue)
    await logger.ainfo(
        "worker_started", task_queue=settings.temporal.task_queue, target=settings.temporal.target
    )
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(worker.run())
            group.create_task(
                run_relay(
                    context,
                    _dispatchers(),
                    poll_interval_seconds=settings.outbox.poll_interval_seconds,
                    batch_size=settings.outbox.batch_size,
                )
            )
    finally:
        await postgres.close_postgres()


if __name__ == "__main__":
    asyncio.run(main())
