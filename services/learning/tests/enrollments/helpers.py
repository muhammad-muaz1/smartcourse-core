import uuid
from datetime import UTC, datetime

from smartcourse_contracts.course_events import CourseEventType, CourseEventV1, CourseSnapshotV1


def course_event(
    course_id: uuid.UUID,
    *,
    revision: int,
    status: str = "published",
    instructor_id: uuid.UUID | None = None,
    max_enrollments: int | None = None,
    title: str = "Distributed Systems",
    event_type: CourseEventType = "course.updated.v1",
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
        ),
    )
