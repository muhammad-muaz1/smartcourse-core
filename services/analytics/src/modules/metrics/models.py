"""Projections: one fact row per entity, built only from events (API design §10).

Facts, not counters. A counter (``total_students += 1``) double-counts on any replay that
gets past the inbox — a backfill, a rebuilt consumer group — and can never be repaired.
A fact row keyed by the entity's id is an idempotent upsert, and every metric is a query
over the facts, so a projection bug is fixed by fixing the query or replaying the log.

``as_of`` on each row is the ``occurred_at`` of the event that last wrote it; an older
event never overwrites a newer one, so replays and backfills can arrive in any order.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class UserFact(Base):
    """No email, no name: analytics counts people, it doesn't contact them."""

    __tablename__ = "user_facts"

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    role: Mapped[str] = mapped_column(String(32), index=True)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CourseFact(Base):
    __tablename__ = "course_facts"

    course_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    instructor_id: Mapped[uuid.UUID] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revision: Mapped[int] = mapped_column(Integer)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EnrollmentFact(Base):
    __tablename__ = "enrollment_facts"
    __table_args__ = (
        Index("ix_enrollment_facts_enrolled_at", "enrolled_at"),
        Index("ix_enrollment_facts_course_id_enrolled_at", "course_id", "enrolled_at"),
        Index("ix_enrollment_facts_student_id", "student_id"),
    )

    enrollment_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    course_id: Mapped[uuid.UUID]
    student_id: Mapped[uuid.UUID]
    status: Mapped[str] = mapped_column(String(32))
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    lessons_total: Mapped[int] = mapped_column(Integer, default=0)
    lessons_completed: Mapped[int] = mapped_column(Integer, default=0)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FailureRecord(Base):
    """Failed events and workflow issues: every ``workflow.failed.v1`` and every dead
    letter, keyed by its own event id so a redelivery records it once."""

    __tablename__ = "failure_records"
    __table_args__ = (Index("ix_failure_records_occurred_at", "occurred_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    # "workflow" | "dead_letter"
    source: Mapped[str] = mapped_column(String(32))
    # For a workflow: the reporting service; for a dead letter: the consumer group.
    service: Mapped[str] = mapped_column(String(64))
    # For a workflow: e.g. "course.publish"; for a dead letter: the original topic.
    subject: Mapped[str] = mapped_column(String(255))
    reference_id: Mapped[str | None] = mapped_column(String(255), default=None)
    step: Mapped[str | None] = mapped_column(String(64), default=None)
    error: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
