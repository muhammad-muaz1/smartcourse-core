import uuid
from datetime import UTC, datetime, timedelta

from smartcourse_contracts.course_events import CourseEventV1, CourseSnapshotV1
from smartcourse_contracts.enrollment_events import (
    EnrollmentEventType,
    EnrollmentEventV1,
    EnrollmentSnapshotV1,
)
from smartcourse_contracts.platform_events import (
    DeadLetterV1,
    WorkflowFailedEventV1,
    WorkflowFailureV1,
)
from smartcourse_contracts.user_events import UserEventV1, UserSnapshotV1
from src.db import postgres
from src.modules.metrics import projections

NOW = datetime.now(tz=UTC)


def user(role: str, *, at: datetime = NOW) -> UserEventV1:
    return UserEventV1(
        event_id=uuid.uuid4(),
        event_type="user.registered.v1",
        occurred_at=at,
        user=UserSnapshotV1(
            user_id=uuid.uuid4(), email="x@example.com", full_name="X", role=role, registered_at=at
        ),
    )


def course(
    course_id: uuid.UUID,
    *,
    instructor_id: uuid.UUID,
    status: str = "published",
    revision: int = 1,
    title: str = "Course",
    at: datetime = NOW,
) -> CourseEventV1:
    return CourseEventV1(
        event_id=uuid.uuid4(),
        event_type="course.published.v1",
        occurred_at=at,
        course=CourseSnapshotV1(
            course_id=course_id,
            revision=revision,
            title=title,
            status=status,
            instructor_id=instructor_id,
        ),
    )


def enrollment(
    enrollment_id: uuid.UUID,
    course_id: uuid.UUID,
    *,
    student_id: uuid.UUID | None = None,
    status: str = "active",
    enrolled_at: datetime = NOW,
    completed_after: timedelta | None = None,
    at: datetime = NOW,
) -> EnrollmentEventV1:
    event_type: EnrollmentEventType = {
        "active": "enrollment.created.v1",
        "withdrawn": "enrollment.withdrawn.v1",
        "completed": "enrollment.completed.v1",
    }[status]  # type: ignore[assignment]  # the dict's values are the Literal members
    return EnrollmentEventV1(
        event_id=uuid.uuid4(),
        event_type=event_type,
        occurred_at=at,
        enrollment=EnrollmentSnapshotV1(
            enrollment_id=enrollment_id,
            student_id=student_id or uuid.uuid4(),
            course_id=course_id,
            course_title="Course",
            instructor_id=uuid.uuid4(),
            status=status,
            enrolled_at=enrolled_at,
            withdrawn_at=at if status == "withdrawn" else None,
            completed_at=enrolled_at + completed_after if completed_after else None,
            lessons_total=2,
            lessons_completed=2 if status == "completed" else 0,
        ),
    )


def workflow_failure(reference_id: str) -> WorkflowFailedEventV1:
    return WorkflowFailedEventV1(
        event_id=uuid.uuid4(),
        event_type="workflow.failed.v1",
        occurred_at=NOW,
        failure=WorkflowFailureV1(
            service="course",
            workflow="course.publish",
            reference_id=reference_id,
            failed_step="validate",
            error="course has no modules",
        ),
    )


def dead_letter() -> DeadLetterV1:
    return DeadLetterV1(
        event_id=uuid.uuid4(),
        original_topic="smartcourse.enrollment.created.v1",
        partition=1,
        offset=42,
        consumer_group="notification",
        attempts=5,
        error="ValidationError: bad payload",
        failed_at=NOW,
        payload="{}",
    )


async def apply(*events: object) -> None:
    """What the consumer does per event, minus Kafka: one transaction each."""
    for event in events:
        async with postgres.database.session_scope() as session:
            if isinstance(event, UserEventV1):
                await projections.apply_user(session, event)
            elif isinstance(event, CourseEventV1):
                await projections.apply_course(session, event)
            elif isinstance(event, EnrollmentEventV1):
                await projections.apply_enrollment(session, event)
            elif isinstance(event, WorkflowFailedEventV1):
                await projections.apply_workflow_failed(session, event)
            elif isinstance(event, DeadLetterV1):
                await projections.apply_dead_letter(session, event, letter_id=uuid.uuid4())
