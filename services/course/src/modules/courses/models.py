"""The course aggregate: a course, its modules, their lessons, and the runs that publish
it. One module because it is one aggregate — every write here checks the course's
ownership and status first, and splitting the tables across modules would mean one
module importing another's models (CLAUDE.md §5).

Status lifecycle (API design §6):

    draft ──publish──▶ publishing ──▶ published ──unpublish──▶ draft
      ▲                    │
      └──── (edit) ── failed ◀┘          any non-publishing state ──archive──▶ archived

Content (modules, lessons) is editable only in ``draft`` or ``failed``: a ``publishing``
course is being validated against a snapshot that must not move, and a ``published``
course is what students see — it must be unpublished to change.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base, TimestampMixin, UUIDMixin


def _enum(enum_cls: type[StrEnum], name: str) -> Enum:
    # values_callable: without it SQLAlchemy stores the member's .name, not its .value,
    # diverging from every JSON representation of the field (docs/adr/0008).
    return Enum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda members: [member.value for member in members],
    )


class CourseLevel(StrEnum):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


class CourseStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    ARCHIVED = "archived"


EDITABLE_CONTENT_STATUSES = frozenset({CourseStatus.DRAFT, CourseStatus.FAILED})
PUBLISHABLE_STATUSES = frozenset({CourseStatus.DRAFT, CourseStatus.FAILED})


class LessonType(StrEnum):
    TEXT = "text"
    VIDEO = "video"


class PublishRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Course(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "courses"
    __table_args__ = (
        CheckConstraint(
            "max_enrollments IS NULL OR max_enrollments > 0", name="max_enrollments_positive"
        ),
    )

    title: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[str] = mapped_column(Text)
    level: Mapped[CourseLevel] = mapped_column(
        _enum(CourseLevel, "course_level"), default=CourseLevel.BEGINNER
    )
    status: Mapped[CourseStatus] = mapped_column(
        _enum(CourseStatus, "course_status"), default=CourseStatus.DRAFT
    )
    # A user id owned by the identity service. No foreign key: users live in another
    # service's database, and cross-service references are by UUID only (CLAUDE.md §6).
    instructor_id: Mapped[uuid.UUID] = mapped_column(index=True)
    # Seat limit enforced by learning against its course_cache. None = unlimited.
    max_enrollments: Mapped[int | None] = mapped_column(Integer, default=None)
    # Bumped on every change that emits a course.* event. Consumers apply an event only
    # if its revision is newer than what they hold, so out-of-order delivery is safe.
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class CourseModule(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "course_modules"
    __table_args__ = (
        # Deferred so a reorder can swap positions inside one transaction; checked at
        # commit, never violated at rest.
        UniqueConstraint(
            "course_id",
            "position",
            name="uq_course_modules_course_id_position",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("position >= 0", name="position_non_negative"),
    )

    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, default=None)
    position: Mapped[int] = mapped_column(Integer)


class Lesson(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "lessons"
    __table_args__ = (
        UniqueConstraint(
            "module_id",
            "position",
            name="uq_lessons_module_id_position",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("position >= 0", name="position_non_negative"),
        CheckConstraint(
            "duration_minutes IS NULL OR duration_minutes >= 0", name="duration_non_negative"
        ),
        # jsonb without a constraint is a dumping ground (CLAUDE.md §6): the body must
        # carry the field its lesson_type promises. Pydantic validates the full shape on
        # the way in; this is the backstop for anything that bypasses the API.
        # IS NOT DISTINCT FROM, not "=": a missing key makes jsonb_typeof NULL, and a
        # CHECK that evaluates to NULL *passes* — "=" would let {} through.
        CheckConstraint(
            "(lesson_type = 'text'"
            " AND jsonb_typeof(body -> 'content') IS NOT DISTINCT FROM 'string')"
            " OR (lesson_type = 'video'"
            " AND jsonb_typeof(body -> 'video_url') IS NOT DISTINCT FROM 'string')",
            name="body_matches_lesson_type",
        ),
    )

    module_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("course_modules.id", ondelete="CASCADE"), index=True
    )
    # Denormalised from the module so course-wide reads are one indexed query.
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(255))
    lesson_type: Mapped[LessonType] = mapped_column(_enum(LessonType, "lesson_type"))
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    duration_minutes: Mapped[int | None] = mapped_column(Integer, default=None)
    position: Mapped[int] = mapped_column(Integer)


class PublishRun(Base, UUIDMixin, TimestampMixin):
    """One attempt to publish a course: the status resource behind the 202."""

    __tablename__ = "publish_runs"
    __table_args__ = (
        # At most one run in flight per course — a double-clicked publish (without an
        # Idempotency-Key) hits this, not a second workflow.
        Index(
            "uq_publish_runs_one_active_per_course",
            "course_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
    )

    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), index=True
    )
    requested_by: Mapped[uuid.UUID]
    status: Mapped[PublishRunStatus] = mapped_column(
        _enum(PublishRunStatus, "publish_run_status"), default=PublishRunStatus.PENDING
    )
    workflow_id: Mapped[str] = mapped_column(String(255))
    # [{"name", "status", "started_at", "finished_at"}] in execution order.
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    failed_step: Mapped[str | None] = mapped_column(String(64), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
