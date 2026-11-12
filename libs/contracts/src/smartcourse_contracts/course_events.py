"""``course.*.v1`` — the facts the course service publishes about a course's catalogue
state, and the delivery seam learning exposes to receive them.

Event-carried state transfer: every event carries the course's full catalogue snapshot,
not a diff, so a consumer never needs to call ``course`` back and can apply events in
any order — ``revision`` increases with every change, and a consumer ignores anything
not newer than what it already holds.

Until Kafka + Schema Registry arrive (docs/adr/0011), these are Pydantic models carried
as JSON through a Temporal activity. Compatibility rules are the same as they will be
for Avro: within ``v1`` only add optional fields; anything else is ``v2``.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# Temporal routing for delivery into learning. The name and queue are the contract —
# course schedules the activity by name and never imports learning's code.
LEARNING_TASK_QUEUE = "learning-tq"
APPLY_COURSE_EVENT_ACTIVITY = "learning.apply_course_event.v1"

CourseEventType = Literal[
    "course.created.v1",
    "course.updated.v1",
    "course.published.v1",
    "course.unpublished.v1",
    "course.archived.v1",
]

# Catalogue status as published on the wire. Deliberately a plain string set, not
# course's internal enum: a consumer must tolerate a status it doesn't know yet.
CourseCatalogStatus = str


class CourseSnapshotV1(BaseModel):
    course_id: uuid.UUID
    revision: int = Field(ge=1)
    title: str
    status: CourseCatalogStatus
    instructor_id: uuid.UUID
    # None = unlimited seats.
    max_enrollments: int | None = None
    published_at: datetime | None = None


class CourseEventV1(BaseModel):
    event_id: uuid.UUID
    event_type: CourseEventType
    occurred_at: datetime
    course: CourseSnapshotV1
