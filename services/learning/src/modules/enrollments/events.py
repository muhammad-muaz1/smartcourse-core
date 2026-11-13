"""Events this module emits, written to the outbox in the caller's transaction — the
enrollment and its event commit together or not at all."""

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.outbox import add_outbox_message
from smartcourse_contracts.enrollment_events import (
    EnrollmentEventType,
    EnrollmentEventV1,
    EnrollmentSnapshotV1,
)
from smartcourse_contracts.topics import topic_for
from src.db.outbox import OutboxMessage
from src.modules.enrollments.models import CourseCache, Enrollment


async def record_enrollment_event(
    session: AsyncSession,
    enrollment: Enrollment,
    course: CourseCache,
    event_type: EnrollmentEventType,
) -> None:
    # Flush so a just-created enrollment has its id before the snapshot.
    await session.flush()
    event = EnrollmentEventV1(
        event_id=uuid.uuid4(),
        event_type=event_type,
        occurred_at=datetime.now(tz=UTC),
        enrollment=EnrollmentSnapshotV1(
            enrollment_id=enrollment.id,
            student_id=enrollment.student_id,
            student_name=enrollment.student_name,
            course_id=course.course_id,
            course_title=course.title,
            instructor_id=course.instructor_id,
            status=enrollment.status.value,
            enrolled_at=enrollment.enrolled_at,
            withdrawn_at=enrollment.withdrawn_at,
            completed_at=enrollment.completed_at,
            lessons_completed=enrollment.lessons_completed,
            lessons_total=len(course.lesson_ids),
        ),
    )
    add_outbox_message(
        session,
        OutboxMessage,
        message_id=event.event_id,
        topic=topic_for(event_type),
        aggregate_id=enrollment.id,
        payload=event.model_dump(mode="json"),
    )
