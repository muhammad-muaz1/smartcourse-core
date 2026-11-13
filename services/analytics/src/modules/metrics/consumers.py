"""Every registered topic analytics projects, discovered by the worker through
``CONSUMERS``. Analytics is the one consumer of everything: a new event type is a new
entry here plus a projection, never a change to a producer."""

from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.kafka import EventMessage, Handler, NonRetryableError
from smartcourse_contracts.course_events import COURSE_TOPICS, CourseEventV1
from smartcourse_contracts.enrollment_events import ENROLLMENT_TOPICS, EnrollmentEventV1
from smartcourse_contracts.platform_events import (
    WORKFLOW_FAILED_TOPIC,
    DeadLetterV1,
    WorkflowFailedEventV1,
)
from smartcourse_contracts.topics import DEAD_LETTER_TOPIC
from smartcourse_contracts.user_events import USER_REGISTERED_TOPIC, UserEventV1
from src.modules.metrics import projections


async def _on_user(session: AsyncSession, message: EventMessage) -> None:
    await projections.apply_user(session, UserEventV1.model_validate_json(message.value))


async def _on_course(session: AsyncSession, message: EventMessage) -> None:
    await projections.apply_course(session, CourseEventV1.model_validate_json(message.value))


async def _on_enrollment(session: AsyncSession, message: EventMessage) -> None:
    await projections.apply_enrollment(
        session, EnrollmentEventV1.model_validate_json(message.value)
    )


async def _on_workflow_failed(session: AsyncSession, message: EventMessage) -> None:
    await projections.apply_workflow_failed(
        session, WorkflowFailedEventV1.model_validate_json(message.value)
    )


async def _on_dead_letter(session: AsyncSession, message: EventMessage) -> None:
    letter_id = message.event_id
    if letter_id is None:
        raise NonRetryableError("dead letter without an event_id header")
    await projections.apply_dead_letter(
        session, DeadLetterV1.model_validate_json(message.value), letter_id=letter_id
    )


CONSUMERS: dict[str, Handler] = {
    USER_REGISTERED_TOPIC: _on_user,
    **dict.fromkeys(COURSE_TOPICS, _on_course),
    **dict.fromkeys(ENROLLMENT_TOPICS, _on_enrollment),
    WORKFLOW_FAILED_TOPIC: _on_workflow_failed,
    DEAD_LETTER_TOPIC: _on_dead_letter,
}
