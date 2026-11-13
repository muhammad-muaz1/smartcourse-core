import uuid
from datetime import UTC, datetime, timedelta

from smartcourse_contracts.enrollment_events import (
    EnrollmentEventType,
    EnrollmentEventV1,
    EnrollmentSnapshotV1,
)
from smartcourse_contracts.user_events import UserEventV1, UserSnapshotV1

MAX_AGE = timedelta(hours=24)


def user_event(
    user_id: uuid.UUID | None = None, *, registered_ago: timedelta = timedelta(0)
) -> UserEventV1:
    when = datetime.now(tz=UTC) - registered_ago
    return UserEventV1(
        event_id=uuid.uuid4(),
        event_type="user.registered.v1",
        occurred_at=when,
        user=UserSnapshotV1(
            user_id=user_id or uuid.uuid4(),
            email="ada@example.com",
            full_name="Ada Lovelace",
            role="student",
            registered_at=when,
        ),
    )


def enrollment_event(
    student_id: uuid.UUID,
    event_type: EnrollmentEventType = "enrollment.created.v1",
) -> EnrollmentEventV1:
    now = datetime.now(tz=UTC)
    completed = event_type == "enrollment.completed.v1"
    return EnrollmentEventV1(
        event_id=uuid.uuid4(),
        event_type=event_type,
        occurred_at=now,
        enrollment=EnrollmentSnapshotV1(
            enrollment_id=uuid.uuid4(),
            student_id=student_id,
            course_id=uuid.uuid4(),
            course_title="Distributed Systems",
            instructor_id=uuid.uuid4(),
            status="completed" if completed else "active",
            enrolled_at=now,
            completed_at=now if completed else None,
            lessons_completed=4 if completed else 0,
            lessons_total=4,
        ),
    )
