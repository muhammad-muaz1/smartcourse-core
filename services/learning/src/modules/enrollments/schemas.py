"""Pydantic response models for the enrollments module. Enrolling and completing a lesson
take no body: the course and lesson come from the path, the student from the token."""

import uuid
from datetime import datetime

from pydantic import BaseModel

from src.modules.enrollments.models import EnrollmentStatus


class EnrollmentPublic(BaseModel):
    id: uuid.UUID
    course_id: uuid.UUID
    # From learning's course_cache at read time — can trail a rename by seconds.
    course_title: str
    student_id: uuid.UUID
    # Snapshot from user.registered.v1; None if learning hasn't heard of the student yet.
    student_name: str | None
    status: EnrollmentStatus
    enrolled_at: datetime
    withdrawn_at: datetime | None
    completed_at: datetime | None
    lessons_completed: int
    lessons_total: int
    progress_percent: float


class ProgressPublic(BaseModel):
    enrollment_id: uuid.UUID
    course_id: uuid.UUID
    status: EnrollmentStatus
    lessons_total: int
    lessons_completed: int
    progress_percent: float
    completed_lesson_ids: list[uuid.UUID]
    completed_at: datetime | None
