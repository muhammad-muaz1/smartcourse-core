"""Enrollments, and ``course_cache`` — the local read model of course that enrollment
reads on its hot path instead of calling the course service (CLAUDE.md §8).
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
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base, TimestampMixin, UUIDMixin

# The status string course publishes for an enrollable course. Compared as a string:
# course's status enum is course's, and the cache must tolerate values it doesn't know.
ENROLLABLE_COURSE_STATUS = "published"


class EnrollmentStatus(StrEnum):
    ACTIVE = "active"
    WITHDRAWN = "withdrawn"


class CourseCache(Base):
    """One row per course, fed only by ``course.*.v1`` events. ``active_enrollments`` is
    the one column learning owns: it's maintained in the enrollment transaction, under
    this row's lock, and never overwritten by an event."""

    __tablename__ = "course_cache"
    __table_args__ = (
        CheckConstraint("active_enrollments >= 0", name="active_enrollments_non_negative"),
    )

    course_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32))
    instructor_id: Mapped[uuid.UUID] = mapped_column(index=True)
    max_enrollments: Mapped[int | None] = mapped_column(Integer, default=None)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Course's revision of the snapshot held here; older events are ignored.
    revision: Mapped[int] = mapped_column(Integer)
    active_enrollments: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Enrollment(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "enrollments"
    __table_args__ = (
        # THE duplicate-enrollment rule (API design §7): at most one *active* enrollment
        # per student per course. Withdrawn rows stay as history, and re-enrolling after
        # a withdrawal is allowed. Enforced here, never by a SELECT first.
        Index(
            "uq_enrollments_active_student_course",
            "student_id",
            "course_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_enrollments_course_id_enrolled_at", "course_id", "enrolled_at"),
        Index("ix_enrollments_student_id_enrolled_at", "student_id", "enrolled_at"),
    )

    # An identity user id: a plain UUID, no foreign key across services.
    student_id: Mapped[uuid.UUID]
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
