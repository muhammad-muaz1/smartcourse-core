"""Enrollment business logic, and keeping ``course_cache`` in step with course.

Enrollment is ONE transaction with no network call (learning CLAUDE.md):

    SELECT course_cache … FOR UPDATE   status check; serialises enrolments per course
    INSERT enrollment                  duplicate → unique-index violation → 409
    seat check, active_enrollments += 1 under the same lock (full → whole tx rolls back)

Every function takes the session first and never commits (CLAUDE.md §6).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Select, and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.exceptions import (
    BusinessRuleError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
)
from platform_core.response import CursorPage, decode_cursor, encode_cursor
from smartcourse_contracts.course_events import CourseEventV1
from src.db.inbox import claim_event
from src.modules.enrollments.models import (
    ENROLLABLE_COURSE_STATUS,
    CourseCache,
    Enrollment,
    EnrollmentStatus,
)
from src.modules.enrollments.schemas import EnrollmentPublic

_ADMIN_ROLE = "admin"  # JWT claim value; identity's enum is never imported (CLAUDE.md §4)
_ACTIVE_ENROLLMENT_INDEX = "uq_enrollments_active_student_course"
COURSE_CACHE_CONSUMER = "learning.course_cache"


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


def _now() -> datetime:
    return datetime.now(tz=UTC)


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
        course_id=course_id,
        status=EnrollmentStatus.ACTIVE,
        enrolled_at=_now(),
    )
    session.add(enrollment)
    try:
        await session.flush()
    except IntegrityError as exc:
        if _ACTIVE_ENROLLMENT_INDEX in str(exc.orig):
            raise AlreadyEnrolledError() from exc
        raise
    if course.max_enrollments is not None and course.active_enrollments >= course.max_enrollments:
        raise CourseFullError()
    course.active_enrollments += 1
    await session.flush()
    return enrollment


async def withdraw(
    session: AsyncSession, enrollment_id: uuid.UUID, *, current_user_id: uuid.UUID, role: str
) -> Enrollment:
    """Idempotent: withdrawing an already-withdrawn enrollment is a no-op."""
    enrollment = (
        await session.execute(
            select(Enrollment).where(Enrollment.id == enrollment_id).with_for_update()
        )
    ).scalar_one_or_none()
    if enrollment is None or (role != _ADMIN_ROLE and enrollment.student_id != current_user_id):
        raise EnrollmentNotFoundError()
    if enrollment.status == EnrollmentStatus.WITHDRAWN:
        return enrollment

    enrollment.status = EnrollmentStatus.WITHDRAWN
    enrollment.withdrawn_at = _now()
    # Atomic decrement — no need to lock the course row to give a seat back.
    await session.execute(
        update(CourseCache)
        .where(CourseCache.course_id == enrollment.course_id)
        .values(active_enrollments=CourseCache.active_enrollments - 1)
    )
    await session.flush()
    return enrollment


# --- reads -------------------------------------------------------------------------


def _public(enrollment: Enrollment, course_title: str) -> EnrollmentPublic:
    return EnrollmentPublic(
        id=enrollment.id,
        course_id=enrollment.course_id,
        course_title=course_title,
        student_id=enrollment.student_id,
        status=enrollment.status,
        enrolled_at=enrollment.enrolled_at,
        withdrawn_at=enrollment.withdrawn_at,
    )


def _base_query() -> Select[tuple[Enrollment, str]]:
    return (
        select(Enrollment, CourseCache.title)
        .join(CourseCache, CourseCache.course_id == Enrollment.course_id)
        .order_by(Enrollment.enrolled_at.desc(), Enrollment.id.desc())
    )


async def _page(
    session: AsyncSession,
    stmt: Select[tuple[Enrollment, str]],
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
    rows = list((await session.execute(stmt.limit(limit + 1))).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        last = rows[-1][0]
        next_cursor = encode_cursor(
            {"enrolled_at": last.enrolled_at.isoformat(), "id": str(last.id)}
        )
    return CursorPage(
        items=[_public(enrollment, title) for enrollment, title in rows],
        next_cursor=next_cursor,
        has_more=has_more,
    )


async def get_enrollment(
    session: AsyncSession, enrollment_id: uuid.UUID, *, current_user_id: uuid.UUID, role: str
) -> EnrollmentPublic:
    """Visible to the student, the course's instructor, and admins. Anyone else gets a
    404, not a 403 — an enrollment id shouldn't confirm someone else's enrollment."""
    row = (await session.execute(_base_query().where(Enrollment.id == enrollment_id))).first()
    if row is None:
        raise EnrollmentNotFoundError()
    enrollment, title = row
    if role != _ADMIN_ROLE and current_user_id != enrollment.student_id:
        instructor_id = await session.scalar(
            select(CourseCache.instructor_id).where(CourseCache.course_id == enrollment.course_id)
        )
        if instructor_id != current_user_id:
            raise EnrollmentNotFoundError()
    return _public(enrollment, title)


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
    if role != _ADMIN_ROLE and user_id != current_user_id:
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
    if role != _ADMIN_ROLE and course.instructor_id != current_user_id:
        raise ForbiddenError("Only the course's instructor or an admin can view its roster")
    stmt = _base_query().where(Enrollment.course_id == course_id)
    if status is not None:
        stmt = stmt.where(Enrollment.status == status)
    return await _page(session, stmt, limit=limit, cursor=cursor)


# --- course_cache ------------------------------------------------------------------


async def apply_course_event(session: AsyncSession, event: CourseEventV1) -> bool:
    """Upsert the course's snapshot. Returns False if the event was a duplicate or older
    than what's held. Two guards, for two different failures:

    - inbox claim on event_id  → the same event delivered twice
    - revision comparison      → two different events delivered out of order
    """
    if not await claim_event(
        session, event_id=event.event_id, consumer_group=COURSE_CACHE_CONSUMER
    ):
        return False

    snapshot = event.course
    values = {
        "title": snapshot.title,
        "status": snapshot.status,
        "instructor_id": snapshot.instructor_id,
        "max_enrollments": snapshot.max_enrollments,
        "published_at": snapshot.published_at,
        "revision": snapshot.revision,
        "synced_at": _now(),
    }
    insert_stmt = insert(CourseCache).values(
        course_id=snapshot.course_id, active_enrollments=0, **values
    )
    upsert = insert_stmt.on_conflict_do_update(
        index_elements=[CourseCache.course_id],
        # active_enrollments is learning's own column — never taken from an event.
        set_=values,
        where=CourseCache.revision < insert_stmt.excluded.revision,
    ).returning(CourseCache.course_id)
    return (await session.execute(upsert)).scalar_one_or_none() is not None
