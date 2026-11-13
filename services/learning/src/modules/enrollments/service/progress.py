"""Lesson completion and the progress view — the enrollment aggregate's other half.

Completing a lesson is one transaction under the enrollment's row lock:

    SELECT enrollment … FOR UPDATE      serialises a student's double-clicks
    INSERT lesson_completion ON CONFLICT DO NOTHING   a repeat is a no-op, not a second count
    recount against course_cache.lesson_ids
    last lesson? → status = completed, completed_at, outbox enrollment.completed.v1

Progress is measured against the course's *current* lesson list (course_cache), so a
lesson removed in a later publish stops counting and a new one has to be done.
Heartbeat-style position updates are not modelled: only completion is a fact anyone else
needs, so only completion emits (learning CLAUDE.md).
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.exceptions import BusinessRuleError, NotFoundError
from src.modules.enrollments.events import record_enrollment_event
from src.modules.enrollments.models import (
    CourseCache,
    Enrollment,
    EnrollmentStatus,
    LessonCompletion,
)
from src.modules.enrollments.schemas import ProgressPublic
from src.modules.enrollments.service.enrollments import (
    EnrollmentNotFoundError,
    ensure_can_view,
    now,
    progress_percent,
)


class LessonNotInCourseError(NotFoundError):
    code = "LESSON_NOT_FOUND"
    message = "This lesson is not part of the course"


class EnrollmentNotActiveError(BusinessRuleError):
    code = "ENROLLMENT_NOT_ACTIVE"
    message = "Progress can only be recorded on an active or completed enrollment"


async def _progress(
    session: AsyncSession, enrollment: Enrollment, course: CourseCache
) -> ProgressPublic:
    completed_ids = set(
        (
            await session.execute(
                select(LessonCompletion.lesson_id).where(
                    LessonCompletion.enrollment_id == enrollment.id
                )
            )
        ).scalars()
    )
    # Reading order, and only lessons still in the course.
    ordered = [uuid.UUID(lid) for lid in course.lesson_ids if uuid.UUID(lid) in completed_ids]
    total = len(course.lesson_ids)
    return ProgressPublic(
        enrollment_id=enrollment.id,
        course_id=enrollment.course_id,
        status=enrollment.status,
        lessons_total=total,
        lessons_completed=enrollment.lessons_completed,
        progress_percent=progress_percent(enrollment.lessons_completed, total),
        completed_lesson_ids=ordered,
        completed_at=enrollment.completed_at,
    )


async def complete_lesson(
    session: AsyncSession,
    enrollment_id: uuid.UUID,
    lesson_id: uuid.UUID,
    *,
    current_user_id: uuid.UUID,
) -> ProgressPublic:
    """Only the student records their own progress. Safe to repeat."""
    enrollment = (
        await session.execute(
            select(Enrollment).where(Enrollment.id == enrollment_id).with_for_update()
        )
    ).scalar_one_or_none()
    if enrollment is None or enrollment.student_id != current_user_id:
        raise EnrollmentNotFoundError()
    if enrollment.status == EnrollmentStatus.WITHDRAWN:
        raise EnrollmentNotActiveError()
    course = await session.get(CourseCache, enrollment.course_id)
    if course is None or str(lesson_id) not in course.lesson_ids:
        raise LessonNotInCourseError()

    await session.execute(
        insert(LessonCompletion)
        .values(enrollment_id=enrollment.id, lesson_id=lesson_id, completed_at=now())
        .on_conflict_do_nothing()
    )
    enrollment.lessons_completed = int(
        await session.scalar(
            select(func.count()).where(
                LessonCompletion.enrollment_id == enrollment.id,
                LessonCompletion.lesson_id.in_([uuid.UUID(lid) for lid in course.lesson_ids]),
            )
        )
        or 0
    )
    if enrollment.status == EnrollmentStatus.ACTIVE and enrollment.lessons_completed >= len(
        course.lesson_ids
    ):
        enrollment.status = EnrollmentStatus.COMPLETED
        enrollment.completed_at = now()
        await record_enrollment_event(session, enrollment, course, "enrollment.completed.v1")
    await session.flush()
    return await _progress(session, enrollment, course)


async def get_progress(
    session: AsyncSession, enrollment_id: uuid.UUID, *, current_user_id: uuid.UUID, role: str
) -> ProgressPublic:
    enrollment = await session.get(Enrollment, enrollment_id)
    if enrollment is None:
        raise EnrollmentNotFoundError()
    await ensure_can_view(session, enrollment, current_user_id=current_user_id, role=role)
    course = await session.get(CourseCache, enrollment.course_id)
    if course is None:  # pragma: no cover - enrollment has a foreign key to course_cache
        raise EnrollmentNotFoundError()
    return await _progress(session, enrollment, course)
