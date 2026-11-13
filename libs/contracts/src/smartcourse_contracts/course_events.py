"""``course.*.v1`` — the facts the course service publishes about a course's catalogue
state.

Event-carried state transfer: every event carries the course's full catalogue snapshot,
not a diff, so a consumer never needs to call ``course`` back and can apply events in
any order — ``revision`` increases with every change, and a consumer ignores anything
not newer than what it already holds.

Compatibility (enforced by tests/test_schemas.py against schemas/): within ``v1`` only
add optional fields; anything else is ``v2``.
"""

import uuid
from datetime import datetime
from typing import Literal, get_args

from pydantic import BaseModel, Field

from smartcourse_contracts.topics import topic_for

CourseEventType = Literal[
    "course.created.v1",
    "course.updated.v1",
    "course.published.v1",
    "course.unpublished.v1",
    "course.archived.v1",
]
COURSE_TOPICS: tuple[str, ...] = tuple(topic_for(t) for t in get_args(CourseEventType))

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
    # Every lesson in the course, in reading order. What learning measures progress and
    # completion against. None = sent by a producer that predates the field.
    lesson_ids: list[uuid.UUID] | None = None


class CourseEventV1(BaseModel):
    event_id: uuid.UUID
    event_type: CourseEventType
    occurred_at: datetime
    course: CourseSnapshotV1
