"""Pydantic request/response models for the courses module."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from src.modules.courses.models import CourseLevel, CourseStatus


class CourseCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=10_000)
    level: CourseLevel = CourseLevel.BEGINNER


class CourseUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, min_length=1, max_length=10_000)
    level: CourseLevel | None = None


class CoursePublic(BaseModel):
    id: uuid.UUID
    title: str
    description: str
    level: CourseLevel
    status: CourseStatus
    instructor_id: uuid.UUID
    created_at: datetime
    updated_at: datetime
