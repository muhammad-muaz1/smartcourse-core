"""Kafka topics this module handles, discovered by the worker through ``CONSUMERS``.

Each handler writes the notification row inside the consumer runner's transaction (after
its inbox claim) and enqueues the Celery delivery only *after commit* — a task must
never race ahead of the row it delivers.
"""

import uuid
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.kafka import EventMessage, Handler, after_commit
from smartcourse_contracts.enrollment_events import EnrollmentEventV1
from smartcourse_contracts.topics import topic_for
from smartcourse_contracts.user_events import USER_REGISTERED_TOPIC, UserEventV1
from src.config import get_settings
from src.modules.notifications import service
from src.modules.notifications.tasks import enqueue_delivery


def _max_age() -> timedelta:
    return timedelta(hours=get_settings().delivery.max_age_hours)


def _deliver_after_commit(session: AsyncSession, notification_id: uuid.UUID | None) -> None:
    if notification_id is not None:
        after_commit(session, lambda: enqueue_delivery(notification_id))


async def _on_user_registered(session: AsyncSession, message: EventMessage) -> None:
    event = UserEventV1.model_validate_json(message.value)
    await service.upsert_recipient(session, event)
    _deliver_after_commit(
        session, await service.notify_account_created(session, event, max_age=_max_age())
    )


async def _on_enrolled(session: AsyncSession, message: EventMessage) -> None:
    event = EnrollmentEventV1.model_validate_json(message.value)
    _deliver_after_commit(
        session, await service.notify_enrolled(session, event, max_age=_max_age())
    )


async def _on_completed(session: AsyncSession, message: EventMessage) -> None:
    event = EnrollmentEventV1.model_validate_json(message.value)
    _deliver_after_commit(
        session, await service.notify_completed(session, event, max_age=_max_age())
    )


CONSUMERS: dict[str, Handler] = {
    USER_REGISTERED_TOPIC: _on_user_registered,
    topic_for("enrollment.created.v1"): _on_enrolled,
    topic_for("enrollment.completed.v1"): _on_completed,
}
