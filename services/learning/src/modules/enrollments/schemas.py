"""Pydantic response models for the enrollments module. Enrolling takes no body: the
course comes from the path, the student from the token."""

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
    status: EnrollmentStatus
    enrolled_at: datetime
    withdrawn_at: datetime | None
