"""Applying events to the fact tables. Called by this module's consumers inside the
consumer runner's transaction, after its inbox claim.

Every apply is an upsert guarded by ``as_of``: an event older than what the row already
reflects is ignored, so a backfill or a replay that interleaves with live traffic can't
move a fact backwards.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from smartcourse_contracts.course_events import CourseEventV1
from smartcourse_contracts.enrollment_events import EnrollmentEventV1
from smartcourse_contracts.platform_events import DeadLetterV1, WorkflowFailedEventV1
from smartcourse_contracts.user_events import UserEventV1
from src.modules.metrics.models import CourseFact, EnrollmentFact, FailureRecord, UserFact


async def _upsert(
    session: AsyncSession,
    model: type[UserFact | CourseFact | EnrollmentFact],
    key: InstrumentedAttribute[uuid.UUID],
    key_value: uuid.UUID,
    values: dict[str, Any],
    *,
    as_of: datetime,
) -> None:
    stmt = insert(model).values({key.key: key_value, **values, "as_of": as_of})
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[key],
            set_={**values, "as_of": as_of},
            where=model.as_of <= stmt.excluded.as_of,
        )
    )


async def apply_user(session: AsyncSession, event: UserEventV1) -> None:
    user = event.user
    await _upsert(
        session,
        UserFact,
        UserFact.user_id,
        user.user_id,
        {"role": user.role, "registered_at": user.registered_at},
        as_of=event.occurred_at,
    )


async def apply_course(session: AsyncSession, event: CourseEventV1) -> None:
    course = event.course
    await _upsert(
        session,
        CourseFact,
        CourseFact.course_id,
        course.course_id,
        {
            "title": course.title,
            "instructor_id": course.instructor_id,
            "status": course.status,
            "published_at": course.published_at,
            "revision": course.revision,
        },
        as_of=event.occurred_at,
    )


async def apply_enrollment(session: AsyncSession, event: EnrollmentEventV1) -> None:
    enrollment = event.enrollment
    await _upsert(
        session,
        EnrollmentFact,
        EnrollmentFact.enrollment_id,
        enrollment.enrollment_id,
        {
            "course_id": enrollment.course_id,
            "student_id": enrollment.student_id,
            "status": enrollment.status,
            "enrolled_at": enrollment.enrolled_at,
            "withdrawn_at": enrollment.withdrawn_at,
            "completed_at": enrollment.completed_at,
            "lessons_total": enrollment.lessons_total,
            "lessons_completed": enrollment.lessons_completed,
        },
        as_of=event.occurred_at,
    )


async def _record_failure(session: AsyncSession, **values: Any) -> None:
    await session.execute(insert(FailureRecord).values(**values).on_conflict_do_nothing())


async def apply_workflow_failed(session: AsyncSession, event: WorkflowFailedEventV1) -> None:
    failure = event.failure
    await _record_failure(
        session,
        id=event.event_id,
        source="workflow",
        service=failure.service,
        subject=failure.workflow,
        reference_id=failure.reference_id,
        step=failure.failed_step,
        error=failure.error,
        occurred_at=event.occurred_at,
    )


async def apply_dead_letter(
    session: AsyncSession, letter: DeadLetterV1, *, letter_id: uuid.UUID
) -> None:
    await _record_failure(
        session,
        id=letter_id,
        source="dead_letter",
        service=letter.consumer_group,
        subject=letter.original_topic,
        reference_id=str(letter.event_id) if letter.event_id else None,
        step=f"partition {letter.partition} offset {letter.offset}",
        error=letter.error,
        occurred_at=letter.failed_at,
    )
