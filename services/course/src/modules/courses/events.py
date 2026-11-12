"""Events this module emits, written to the outbox in the caller's transaction.

``course.*.v1`` carry the course's full catalogue snapshot (smartcourse_contracts).
``course.publish_requested.v1`` is internal: the relay turns it into a
``PublishCourseWorkflow`` start, so the API never talks to Temporal and a publish
request commits atomically with the run row it creates.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from smartcourse_contracts.course_events import CourseEventType, CourseEventV1, CourseSnapshotV1
from src.db.outbox import add_outbox_message
from src.modules.courses.models import Course, PublishRun

PUBLISH_REQUESTED_TOPIC = "smartcourse.course.publish_requested.v1"


def topic_for(event_type: CourseEventType) -> str:
    """``course.published.v1`` → ``smartcourse.course.published.v1``."""
    return f"smartcourse.{event_type}"


async def record_course_event(
    session: AsyncSession, course: Course, event_type: CourseEventType
) -> CourseEventV1:
    course.revision += 1
    # Flush so a just-created course has its id and timestamps before the snapshot.
    await session.flush()
    event = CourseEventV1(
        event_id=uuid.uuid4(),
        event_type=event_type,
        occurred_at=datetime.now(tz=UTC),
        course=CourseSnapshotV1(
            course_id=course.id,
            revision=course.revision,
            title=course.title,
            status=course.status.value,
            instructor_id=course.instructor_id,
            max_enrollments=course.max_enrollments,
            published_at=course.published_at,
        ),
    )
    add_outbox_message(
        session,
        message_id=event.event_id,
        topic=topic_for(event_type),
        aggregate_id=course.id,
        payload=event.model_dump(mode="json"),
    )
    return event


def record_publish_requested(session: AsyncSession, run: PublishRun) -> None:
    add_outbox_message(
        session,
        message_id=uuid.uuid4(),
        topic=PUBLISH_REQUESTED_TOPIC,
        aggregate_id=run.course_id,
        payload={"run_id": str(run.id), "course_id": str(run.course_id)},
    )
