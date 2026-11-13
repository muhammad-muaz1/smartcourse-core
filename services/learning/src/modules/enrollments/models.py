"""Enrollments and their progress, plus the two local read models enrollment reads on its
hot path instead of calling another service (CLAUDE.md §8):

- ``course_cache``  — fed by ``course.*.v1``: status, seats, instructor, lesson list
- ``student_directory`` — fed by ``user.registered.v1``: a display name for rosters

Progress lives with the enrollment because they are one aggregate: enrolling initialises
it, the last lesson completed completes the enrollment, in one transaction (CLAUDE.md §2).
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base, TimestampMixin, UUIDMixin

# The status string course publishes for an enrollable course. Compared as a string:
# course's status enum is course's, and the cache must tolerate values it doesn't know.
ENROLLABLE_COURSE_STATUS = "published"


class EnrollmentStatus(StrEnum):
    ACTIVE = "active"
    WITHDRAWN = "withdrawn"
    COMPLETED = "completed"


class CourseCache(Base):
    """One row per course, fed only by ``course.*.v1`` events. ``active_enrollments`` is
    the one column learning owns: it's maintained in the enrollment transaction, under
    this row's lock, and never overwritten by an event."""

    __tablename__ = "course_cache"
    __table_args__ = (
        CheckConstraint("active_enrollments >= 0", name="active_enrollments_non_negative"),
        CheckConstraint("jsonb_typeof(lesson_ids) = 'array'", name="lesson_ids_is_array"),
    )

    course_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32))
    instructor_id: Mapped[uuid.UUID] = mapped_column(index=True)
    max_enrollments: Mapped[int | None] = mapped_column(Integer, default=None)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Every lesson id (as strings), in reading order, from the latest snapshot.
    lesson_ids: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    # Course's revision of the snapshot held here; older events are ignored.
    revision: Mapped[int] = mapped_column(Integer)
    active_enrollments: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StudentDirectory(Base):
    """Display names only — never the email the event also carries (personal data this
    service has no use for)."""

    __tablename__ = "student_directory"

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Enrollment(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "enrollments"
    __table_args__ = (
        # THE duplicate-enrollment rule (API design §7): at most one enrollment per
        # student per course that isn't withdrawn — active or completed. Withdrawn rows
        # stay as history, and re-enrolling after a withdrawal is allowed. Enforced here,
        # never by a SELECT first.
        Index(
            "uq_enrollments_current_student_course",
            "student_id",
            "course_id",
            unique=True,
            postgresql_where=text("status <> 'withdrawn'"),
        ),
        Index("ix_enrollments_course_id_enrolled_at", "course_id", "enrolled_at"),
        Index("ix_enrollments_student_id_enrolled_at", "student_id", "enrolled_at"),
        CheckConstraint("lessons_completed >= 0", name="lessons_completed_non_negative"),
        CheckConstraint(
            "(status = 'completed') = (completed_at IS NOT NULL)",
            name="completed_at_iff_completed",
        ),
    )

    # An identity user id: a plain UUID, no foreign key across services.
    student_id: Mapped[uuid.UUID]
    # Snapshot at enrollment time, from student_directory. Stale names are acceptable;
    # a call to identity for a roster is not (CLAUDE.md §8).
    student_name: Mapped[str | None] = mapped_column(String(255), default=None)
    course_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("course_cache.course_id"))
    status: Mapped[EnrollmentStatus] = mapped_column(
        Enum(
            EnrollmentStatus,
            name="enrollment_status",
            native_enum=True,
            values_callable=lambda members: [member.value for member in members],
        ),
        default=EnrollmentStatus.ACTIVE,
    )
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Progress initialised to 0 in the enrollment transaction; maintained with each
    # lesson completion under the enrollment's row lock.
    lessons_completed: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


class LessonCompletion(Base):
    """One row per lesson a student finished in one enrollment. The primary key makes a
    repeated completion a no-op rather than a second count."""

    __tablename__ = "lesson_completions"

    enrollment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("enrollments.id", ondelete="CASCADE"), primary_key=True
    )
    # A course-service lesson id: plain UUID, validated against course_cache.lesson_ids.
    lesson_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
