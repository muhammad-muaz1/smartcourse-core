"""Celery tasks for this module. Imported by ``src.celery_app``'s discovery, which is what
registers them and their ``BEAT_SCHEDULE``; enqueued by the consumers after their
transaction commits.

``deliver`` is one delivery attempt: late-acked, time-limited, retried with jittered
exponential backoff up to ``celery.max_retries``. The final failure is recorded on the
row and reported as ``workflow.failed.v1`` — nothing fails silently.
"""

import asyncio
import random
import uuid
from datetime import timedelta

import structlog
from celery import Task

from platform_core.metrics import TASKS_COMPLETED
from src.celery_app import MAINTENANCE_QUEUE, NOTIFICATIONS_QUEUE, celery_app, run_async, settings
from src.db.postgres import database
from src.modules.notifications import service

logger = structlog.get_logger(__name__)

DELIVER_TASK = "notification.deliver.v1"
SWEEP_TASK = "notification.requeue_stale.v1"

_mailer = service.LogMailer()


def _backoff(retries: int) -> int:
    """Full jitter: a broker-wide hiccup doesn't come back as a synchronised herd."""
    ceiling = min(
        settings.celery.retry_backoff_max_seconds,
        settings.celery.retry_backoff_seconds * 2**retries,
    )
    return random.randint(1, max(1, ceiling))  # noqa: S311 - jitter, not cryptography


async def _deliver(notification_id: uuid.UUID) -> str:
    async with database.session_scope() as session:
        return await service.deliver(session, notification_id, mailer=_mailer)


async def _record_failure(notification_id: uuid.UUID, error: str, *, final: bool) -> None:
    async with database.session_scope() as session:
        await service.record_failed_attempt(session, notification_id, error=error, final=final)


@celery_app.task(
    name=DELIVER_TASK,
    bind=True,
    acks_late=True,
    max_retries=settings.celery.max_retries,
    soft_time_limit=settings.celery.soft_time_limit_seconds,
    time_limit=settings.celery.time_limit_seconds,
)
def deliver(self: Task, notification_id: str) -> str:
    target = uuid.UUID(notification_id)
    try:
        outcome = run_async(_deliver(target))
    except service.NotificationNotFoundError:
        # Nothing to retry towards: the row was never committed.
        TASKS_COMPLETED.labels(task=DELIVER_TASK, outcome="failed").inc()
        return "missing"
    except Exception as exc:  # noqa: BLE001 - any delivery failure is retried, then recorded
        error = f"{type(exc).__name__}: {exc}"
        final = self.request.retries >= settings.celery.max_retries
        run_async(_record_failure(target, error, final=final))
        if final:
            TASKS_COMPLETED.labels(task=DELIVER_TASK, outcome="failed").inc()
            run_async(logger.aerror("notification_failed", notification_id=notification_id))
            return "failed"
        TASKS_COMPLETED.labels(task=DELIVER_TASK, outcome="retried").inc()
        raise self.retry(exc=exc, countdown=_backoff(self.request.retries)) from exc
    TASKS_COMPLETED.labels(task=DELIVER_TASK, outcome="succeeded").inc()
    return outcome


async def enqueue_delivery(notification_id: uuid.UUID) -> None:
    """From async code (a consumer's after-commit callback). ``apply_async`` talks to the
    broker synchronously, so it runs on a thread — ``to_thread`` copies the context, so
    the trace continues into the task."""
    await asyncio.to_thread(
        deliver.apply_async, args=[str(notification_id)], queue=NOTIFICATIONS_QUEUE
    )


async def _stale_ids() -> list[uuid.UUID]:
    async with database.session_scope() as session:
        return await service.stale_pending_ids(
            session, older_than=timedelta(seconds=settings.celery.stale_pending_seconds)
        )


@celery_app.task(name=SWEEP_TASK, acks_late=True, soft_time_limit=50, time_limit=60)
def requeue_stale() -> int:
    """The recovery path for a lost enqueue. Delivery is idempotent, so re-enqueueing a
    row that is merely slow costs one no-op attempt."""
    ids = run_async(_stale_ids())
    for notification_id in ids:
        deliver.apply_async(args=[str(notification_id)], queue=NOTIFICATIONS_QUEUE)
    if ids:
        run_async(logger.awarning("notifications_requeued", count=len(ids)))
    TASKS_COMPLETED.labels(task=SWEEP_TASK, outcome="succeeded").inc()
    return len(ids)


BEAT_SCHEDULE = {
    "requeue-stale-notifications": {
        "task": SWEEP_TASK,
        "schedule": float(settings.celery.sweep_interval_seconds),
        "options": {"queue": MAINTENANCE_QUEUE},
    }
}
celery_app.conf.beat_schedule.update(BEAT_SCHEDULE)
