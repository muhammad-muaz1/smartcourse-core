"""Kafka topics this module handles, discovered by the worker through ``CONSUMERS``
(topic → handler). The consumer runner (platform_core.kafka) owns the transaction, the
inbox claim, retries and dead-lettering; a handler only parses and applies."""

from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.kafka import EventMessage, Handler
from smartcourse_contracts.course_events import COURSE_TOPICS, CourseEventV1
from smartcourse_contracts.user_events import USER_REGISTERED_TOPIC, UserEventV1
from src.modules.enrollments.service import read_models


async def _on_course_event(session: AsyncSession, message: EventMessage) -> None:
    await read_models.apply_course_event(session, CourseEventV1.model_validate_json(message.value))


async def _on_user_registered(session: AsyncSession, message: EventMessage) -> None:
    await read_models.apply_user_registered(session, UserEventV1.model_validate_json(message.value))


CONSUMERS: dict[str, Handler] = {
    **dict.fromkeys(COURSE_TOPICS, _on_course_event),
    USER_REGISTERED_TOPIC: _on_user_registered,
}
