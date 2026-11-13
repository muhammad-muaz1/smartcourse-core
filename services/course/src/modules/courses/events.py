"""Events this module emits, written to the outbox in the caller's transaction.

- ``course.*.v1`` carry the course's full catalogue snapshot (smartcourse_contracts),
  delivered to Kafka by the relay.
- ``workflow.failed.v1`` reports a publish saga that compensated, for analytics' failure
  metric.
- ``course.publish_requested.v1`` is internal: the relay turns it into a
  ``PublishCourseWorkflow`` start, so the API never talks to Temporal and a publish
  request commits atomically with the run row it creates.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smartcourse_contracts.course_events import CourseEventType, CourseEventV1, CourseSnapshotV1
from smartcourse_contracts.platform_events import (
    WORKFLOW_FAILED_TOPIC,
    WorkflowFailedEventV1,
    WorkflowFailureV1,
)
from smartcourse_contracts.topics import topic_for
from src.db.outbox import add_outbox_message
from src.modules.courses.models import Course, CourseModule, Lesson, PublishRun

# Internal: never leaves this service, so it isn't in the contract registry and never
# reaches Kafka.
PUBLISH_REQUESTED_TOPIC = "smartcourse.course.publish_requested.v1"
PUBLISH_WORKFLOW = "course.publish"


async def _lesson_ids(session: AsyncSession, course_id: uuid.UUID) -> list[uuid.UUID]:
    """Every lesson in reading order — what learning measures progress against."""
    rows = await session.execute(
        select(Lesson.id)
        .join(CourseModule, CourseModule.id == Lesson.module_id)
        .where(Lesson.course_id == course_id)
        .order_by(CourseModule.position, Lesson.position)
    )
    return list(rows.scalars())


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
            lesson_ids=await _lesson_ids(session, course.id),
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


def record_publish_failed(session: AsyncSession, run: PublishRun) -> None:
    event = WorkflowFailedEventV1(
        event_id=uuid.uuid4(),
        event_type="workflow.failed.v1",
        occurred_at=datetime.now(tz=UTC),
        failure=WorkflowFailureV1(
            service="course",
            workflow=PUBLISH_WORKFLOW,
            reference_id=str(run.id),
            failed_step=run.failed_step,
            error=run.error or "unknown error",
        ),
    )
    add_outbox_message(
        session,
        message_id=event.event_id,
        topic=WORKFLOW_FAILED_TOPIC,
        aggregate_id=run.course_id,
        payload=event.model_dump(mode="json"),
    )
