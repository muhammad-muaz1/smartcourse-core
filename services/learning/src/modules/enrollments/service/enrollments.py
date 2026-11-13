"""Enrol, withdraw, and the enrollment reads.

Enrollment is ONE transaction with no network call (learning CLAUDE.md):

    SELECT course_cache … FOR UPDATE   status check; serialises enrolments per course
    INSERT enrollment                  duplicate → unique-index violation → 409
                                       progress initialised (lessons_completed = 0)
    seat check, active_enrollments += 1 under the same lock (full → whole tx rolls back)
    INSERT outbox enrollment.created.v1

Every function takes the session first and never commits (CLAUDE.md §6).
"""

import uuid
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import Select, and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.exceptions import (
    BusinessRuleError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
)
from platform_core.response import CursorPage, decode_cursor, encode_cursor
from src.modules.enrollments.events import record_enrollment_event
from src.modules.enrollments.models import (
    ENROLLABLE_COURSE_STATUS,
    CourseCache,
    Enrollment,
    EnrollmentStatus,
    StudentDirectory,
)
from src.modules.enrollments.schemas import EnrollmentPublic

ADMIN_ROLE = "admin"  # JWT claim value; identity's enum is never imported (CLAUDE.md §4)
_CURRENT_ENROLLMENT_INDEX = "uq_enrollments_current_student_course"


class CourseNotFoundError(NotFoundError):
    code = "COURSE_NOT_FOUND"
    message = "Course not found"


class CourseNotEnrollableError(BusinessRuleError):
    code = "COURSE_NOT_ENROLLABLE"


class CourseFullError(ConflictError):
    code = "COURSE_FULL"
    message = "This course has no seats left"


class AlreadyEnrolledError(ConflictError):
    code = "ALREADY_ENROLLED"
    message = "You are already enrolled in this course"


class EnrollmentNotFoundError(NotFoundError):
    code = "ENROLLMENT_NOT_FOUND"
    message = "Enrollment not found"


class EnrollmentCompletedError(ConflictError):
    code = "ENROLLMENT_COMPLETED"
    message = "A completed enrollment can't be withdrawn"


def now() -> datetime:
    return datetime.now(tz=UTC)


def progress_percent(completed: int, total: int) -> float:
    return round(100 * completed / total, 1) if total else 0.0


# --- enrol / withdraw --------------------------------------------------------------


async def enroll(
    session: AsyncSession, *, course_id: uuid.UUID, student_id: uuid.UUID
) -> Enrollment:
    course = (
        await session.execute(
            select(CourseCache).where(CourseCache.course_id == course_id).with_for_update()
        )
    ).scalar_one_or_none()
    if course is None:
        # Either no such course, or course's event hasn't reached us yet — from the
        # student's side they're the same: not available.
        raise CourseNotFoundError()
    if course.status != ENROLLABLE_COURSE_STATUS:
        raise CourseNotEnrollableError(
            f"This course is not open for enrollment (status: {course.status})"
        )

    # Insert before the seat check, so an already-enrolled student retrying against a
    # full course hears "already enrolled", not "full". If the seat check then fails,
    # raising rolls this insert back with the rest of the transaction.
    enrollment = Enrollment(
        student_id=student_id,
        student_name=await session.scalar(
            select(StudentDirectory.full_name).where(StudentDirectory.user_id == student_id)
        ),
        course_id=course_id,
        status=EnrollmentStatus.ACTIVE,
        enrolled_at=now(),
        # Progress tracking starts here, in the same transaction (brief §3).
        lessons_completed=0,
    )
    session.add(enrollment)
    try:
        await session.flush()
    except IntegrityError as exc:
        if _CURRENT_ENROLLMENT_INDEX in str(exc.orig):
            raise AlreadyEnrolledError() from exc
        raise
    if course.max_enrollments is not None and course.active_enrollments >= course.max_enrollments:
        raise CourseFullError()
    course.active_enrollments += 1
    await record_enrollment_event(session, enrollment, course, "enrollment.created.v1")
    return enrollment


async def withdraw(
    session: AsyncSession, enrollment_id: uuid.UUID, *, current_user_id: uuid.UUID, role: str
) -> Enrollment:
    """Idempotent: withdrawing an already-withdrawn enrollment is a no-op (and emits
    nothing). A completed enrollment is history the student earned; it isn't withdrawn."""
    enrollment = (
        await session.execute(
            select(Enrollment).where(Enrollment.id == enrollment_id).with_for_update()
        )
    ).scalar_one_or_none()
    if enrollment is None or (role != ADMIN_ROLE and enrollment.student_id != current_user_id):
        raise EnrollmentNotFoundError()
    if enrollment.status == EnrollmentStatus.WITHDRAWN:
        return enrollment
    if enrollment.status == EnrollmentStatus.COMPLETED:
        raise EnrollmentCompletedError()

    enrollment.status = EnrollmentStatus.WITHDRAWN
    enrollment.withdrawn_at = now()
    # Atomic decrement, returning the row for the event snapshot — no lock needed to
    # give a seat back.
    course = (
        await session.execute(
            update(CourseCache)
            .where(CourseCache.course_id == enrollment.course_id)
            .values(active_enrollments=CourseCache.active_enrollments - 1)
            .returning(CourseCache)
        )
    ).scalar_one()
    await record_enrollment_event(session, enrollment, course, "enrollment.withdrawn.v1")
    return enrollment


# --- reads -------------------------------------------------------------------------

_Row = tuple[Enrollment, str, list[str], str | None]


def _public(row: _Row) -> EnrollmentPublic:
    enrollment, course_title, lesson_ids, directory_name = row
    total = len(lesson_ids)
    return EnrollmentPublic(
        id=enrollment.id,
        course_id=enrollment.course_id,
        course_title=course_title,
        student_id=enrollment.student_id,
        # The enrollment-time snapshot, else the directory: a student who enrolled in
        # the second before their registration event arrived still gets a name.
        student_name=enrollment.student_name or directory_name,
        status=enrollment.status,
        enrolled_at=enrollment.enrolled_at,
        withdrawn_at=enrollment.withdrawn_at,
        completed_at=enrollment.completed_at,
        lessons_completed=enrollment.lessons_completed,
        lessons_total=total,
        progress_percent=progress_percent(enrollment.lessons_completed, total),
    )


def _base_query() -> Select[_Row]:
    # cast: the outer join makes the directory name nullable, which the column's own
    # (non-null) type can't express.
    return cast(
        Select[_Row],
        select(Enrollment, CourseCache.title, CourseCache.lesson_ids, StudentDirectory.full_name)
        .join(CourseCache, CourseCache.course_id == Enrollment.course_id)
        .outerjoin(StudentDirectory, StudentDirectory.user_id == Enrollment.student_id)
        .order_by(Enrollment.enrolled_at.desc(), Enrollment.id.desc()),
    )


async def _page(
    session: AsyncSession,
    stmt: Select[_Row],
    *,
    limit: int,
    cursor: str | None,
) -> CursorPage[EnrollmentPublic]:
    """Keyset pagination on (enrolled_at DESC, id DESC) — same scheme as the catalogue."""
    if cursor:
        position = decode_cursor(cursor)
        at = datetime.fromisoformat(position["enrolled_at"])
        last_id = uuid.UUID(position["id"])
        stmt = stmt.where(
            or_(
                Enrollment.enrolled_at < at,
                and_(Enrollment.enrolled_at == at, Enrollment.id < last_id),
            )
        )
    rows = list((await session.execute(stmt.limit(limit + 1))).tuples())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        last = rows[-1][0]
        next_cursor = encode_cursor(
            {"enrolled_at": last.enrolled_at.isoformat(), "id": str(last.id)}
        )
    return CursorPage(
        items=[_public(row) for row in rows], next_cursor=next_cursor, has_more=has_more
    )


async def ensure_can_view(
    session: AsyncSession, enrollment: Enrollment, *, current_user_id: uuid.UUID, role: str
) -> None:
    """The student, the course's instructor, and admins. Anyone else gets a 404, not a
    403 — an enrollment id shouldn't confirm someone else's enrollment."""
    if role == ADMIN_ROLE or current_user_id == enrollment.student_id:
        return
    instructor_id = await session.scalar(
        select(CourseCache.instructor_id).where(CourseCache.course_id == enrollment.course_id)
    )
    if instructor_id != current_user_id:
        raise EnrollmentNotFoundError()


async def get_enrollment(
    session: AsyncSession, enrollment_id: uuid.UUID, *, current_user_id: uuid.UUID, role: str
) -> EnrollmentPublic:
    row = (
        (await session.execute(_base_query().where(Enrollment.id == enrollment_id)))
        .tuples()
        .first()
    )
    if row is None:
        raise EnrollmentNotFoundError()
    await ensure_can_view(session, row[0], current_user_id=current_user_id, role=role)
    return _public(row)


async def list_for_student(
    session: AsyncSession,
    *,
    student_id: uuid.UUID,
    status: EnrollmentStatus | None,
    limit: int,
    cursor: str | None,
) -> CursorPage[EnrollmentPublic]:
    stmt = _base_query().where(Enrollment.student_id == student_id)
    if status is not None:
        stmt = stmt.where(Enrollment.status == status)
    return await _page(session, stmt, limit=limit, cursor=cursor)


async def enrollment_history(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    current_user_id: uuid.UUID,
    role: str,
    limit: int,
    cursor: str | None,
) -> CursorPage[EnrollmentPublic]:
    """Every enrollment the user ever had, withdrawals included. Self or admin."""
    if role != ADMIN_ROLE and user_id != current_user_id:
        raise ForbiddenError("You can only view your own enrollment history")
    return await list_for_student(
        session, student_id=user_id, status=None, limit=limit, cursor=cursor
    )


async def course_roster(
    session: AsyncSession,
    course_id: uuid.UUID,
    *,
    current_user_id: uuid.UUID,
    role: str,
    status: EnrollmentStatus | None,
    limit: int,
    cursor: str | None,
) -> CursorPage[EnrollmentPublic]:
    course = await session.get(CourseCache, course_id)
    if course is None:
        raise CourseNotFoundError()
    if role != ADMIN_ROLE and course.instructor_id != current_user_id:
        raise ForbiddenError("Only the course's instructor or an admin can view its roster")
    stmt = _base_query().where(Enrollment.course_id == course_id)
    if status is not None:
        stmt = stmt.where(Enrollment.status == status)
    return await _page(session, stmt, limit=limit, cursor=cursor)
