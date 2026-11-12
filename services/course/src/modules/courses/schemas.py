"""Pydantic request/response models for the courses module."""

import uuid
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, Field, HttpUrl, model_validator

from src.modules.courses.models import (
    CourseLevel,
    CourseStatus,
    LessonType,
    PublishRunStatus,
)

# --- courses -----------------------------------------------------------------------


class CourseCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=10_000)
    level: CourseLevel = CourseLevel.BEGINNER
    # Omit or null for unlimited seats.
    max_enrollments: int | None = Field(default=None, gt=0, le=1_000_000)


class CourseUpdate(BaseModel):
    """Partial update. ``max_enrollments: null`` explicitly removes the seat limit;
    omitting the field leaves it unchanged."""

    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, min_length=1, max_length=10_000)
    level: CourseLevel | None = None
    max_enrollments: int | None = Field(default=None, gt=0, le=1_000_000)


class CoursePublic(BaseModel):
    id: uuid.UUID
    title: str
    description: str
    level: CourseLevel
    status: CourseStatus
    instructor_id: uuid.UUID
    max_enrollments: int | None
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime


# --- lessons -----------------------------------------------------------------------


class TextLessonBody(BaseModel):
    content: str = Field(min_length=1, max_length=200_000, description="Markdown")


class VideoLessonBody(BaseModel):
    video_url: HttpUrl
    transcript: str | None = Field(default=None, max_length=200_000)


_BODY_MODELS: dict[LessonType, type[BaseModel]] = {
    LessonType.TEXT: TextLessonBody,
    LessonType.VIDEO: VideoLessonBody,
}


def validate_lesson_body(lesson_type: LessonType, body: dict[str, Any]) -> dict[str, Any]:
    """The body's shape is decided by ``lesson_type``. Returns the normalised JSON that
    is stored (unknown keys dropped)."""
    model = _BODY_MODELS[lesson_type].model_validate(body)
    return model.model_dump(mode="json", exclude_none=True)


class LessonCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    lesson_type: LessonType
    body: dict[str, Any]
    duration_minutes: int | None = Field(default=None, ge=0, le=10_000)

    @model_validator(mode="after")
    def _body_matches_type(self) -> Self:
        self.body = validate_lesson_body(self.lesson_type, self.body)
        return self


class LessonUpdate(BaseModel):
    """Partial update. To change a lesson's type, send ``lesson_type`` and a matching
    ``body`` together; sending ``body`` alone validates it against the current type."""

    title: str | None = Field(default=None, min_length=1, max_length=255)
    lesson_type: LessonType | None = None
    body: dict[str, Any] | None = None
    duration_minutes: int | None = Field(default=None, ge=0, le=10_000)

    @model_validator(mode="after")
    def _type_needs_body(self) -> Self:
        if self.lesson_type is not None and self.body is None:
            raise ValueError("changing lesson_type requires a body for the new type")
        if self.lesson_type is not None and self.body is not None:
            self.body = validate_lesson_body(self.lesson_type, self.body)
        return self


class LessonSummary(BaseModel):
    id: uuid.UUID
    title: str
    lesson_type: LessonType
    duration_minutes: int | None
    position: int


class LessonPublic(LessonSummary):
    module_id: uuid.UUID
    course_id: uuid.UUID
    body: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class LessonOrder(BaseModel):
    lesson_ids: list[uuid.UUID] = Field(min_length=1)


# --- modules -----------------------------------------------------------------------


class ModuleCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=10_000)


class ModuleUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=10_000)


class ModulePublic(BaseModel):
    id: uuid.UUID
    course_id: uuid.UUID
    title: str
    description: str | None
    position: int
    lessons: list[LessonSummary] = Field(default_factory=list)


class ModuleOrder(BaseModel):
    module_ids: list[uuid.UUID] = Field(min_length=1)


class CourseDetail(CoursePublic):
    modules: list[ModulePublic]


# --- publishing --------------------------------------------------------------------


class PublishStep(BaseModel):
    name: str
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None


class PublishRunPublic(BaseModel):
    id: uuid.UUID
    course_id: uuid.UUID
    requested_by: uuid.UUID
    status: PublishRunStatus
    workflow_id: str
    steps: list[PublishStep]
    failed_step: str | None
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
