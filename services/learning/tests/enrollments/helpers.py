import uuid
from datetime import UTC, datetime

from smartcourse_contracts.course_events import CourseEventType, CourseEventV1, CourseSnapshotV1
from smartcourse_contracts.user_events import UserEventV1, UserSnapshotV1
from src.db import postgres
from src.modules.enrollments.service import read_models


def course_event(
    course_id: uuid.UUID,
    *,
    revision: int,
    status: str = "published",
    instructor_id: uuid.UUID | None = None,
    max_enrollments: int | None = None,
    title: str = "Distributed Systems",
    event_type: CourseEventType = "course.updated.v1",
    lesson_ids: list[uuid.UUID] | None = None,
) -> CourseEventV1:
    return CourseEventV1(
        event_id=uuid.uuid4(),
        event_type=event_type,
        occurred_at=datetime.now(tz=UTC),
        course=CourseSnapshotV1(
            course_id=course_id,
            revision=revision,
            title=title,
            status=status,
            instructor_id=instructor_id or uuid.uuid4(),
            max_enrollments=max_enrollments,
            lesson_ids=lesson_ids,
        ),
    )


def user_event(user_id: uuid.UUID, *, full_name: str, role: str = "student") -> UserEventV1:
    return UserEventV1(
        event_id=uuid.uuid4(),
        event_type="user.registered.v1",
        occurred_at=datetime.now(tz=UTC),
        user=UserSnapshotV1(
            user_id=user_id,
            email=f"{user_id}@example.com",
            full_name=full_name,
            role=role,
            registered_at=datetime.now(tz=UTC),
        ),
    )


async def apply_course_event(event: CourseEventV1) -> bool:
    """What the Kafka consumer does, minus the transport: one transaction per event."""
    async with postgres.database.session_scope() as session:
        return await read_models.apply_course_event(session, event)


async def apply_user_event(event: UserEventV1) -> bool:
    async with postgres.database.session_scope() as session:
        return await read_models.apply_user_registered(session, event)
