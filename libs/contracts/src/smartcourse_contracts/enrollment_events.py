"""``enrollment.*.v1`` — learning's facts about one student's enrollment in one course.

Event-carried state, like ``course.*``: each event carries the enrollment's full snapshot,
so notification can greet the student by course title and analytics can compute
completion times without calling learning. Partition key is the enrollment id, so one
enrollment's events arrive in order.

``enrollment.completed.v1`` is the brief's "course completed" fact. It is named for the
aggregate that owns it — learning's enrollment — because ``smartcourse.course.*`` topics
belong to the course service.
"""

import uuid
from datetime import datetime
from typing import Literal, get_args

from pydantic import BaseModel

from smartcourse_contracts.topics import topic_for

EnrollmentEventType = Literal[
    "enrollment.created.v1",
    "enrollment.withdrawn.v1",
    "enrollment.completed.v1",
]
ENROLLMENT_TOPICS: tuple[str, ...] = tuple(topic_for(t) for t in get_args(EnrollmentEventType))


class EnrollmentSnapshotV1(BaseModel):
    enrollment_id: uuid.UUID
    student_id: uuid.UUID
    # Snapshot taken at enrollment time; None if learning hadn't heard of the student yet.
    student_name: str | None = None
    course_id: uuid.UUID
    course_title: str
    instructor_id: uuid.UUID
    # Plain string: active | withdrawn | completed today.
    status: str
    enrolled_at: datetime
    withdrawn_at: datetime | None = None
    completed_at: datetime | None = None
    lessons_completed: int = 0
    lessons_total: int = 0


class EnrollmentEventV1(BaseModel):
    event_id: uuid.UUID
    event_type: EnrollmentEventType
    occurred_at: datetime
    enrollment: EnrollmentSnapshotV1
