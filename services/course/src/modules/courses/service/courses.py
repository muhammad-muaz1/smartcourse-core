"""Course catalogue and lifecycle: create, browse, read, update metadata, unpublish,
archive. Ownership checks (owning instructor or admin) live here per CLAUDE.md §9, never
as an inline role check in routes.

Every function takes the session as its first argument rather than fetching one, so the
same function is callable from a route, a Temporal activity, or a consumer unchanged.
Every state change that other services care about writes a ``course.*.v1`` outbox row
in the same transaction (events.py).
"""

import uuid
from datetime import datetime
from types import EllipsisType

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.exceptions import ConflictError, ForbiddenError, NotFoundError
from platform_core.response import CursorPage, decode_cursor, encode_cursor
from src.modules.courses.events import record_course_event
from src.modules.courses.models import (
    EDITABLE_CONTENT_STATUSES,
    Course,
    CourseLevel,
    CourseStatus,
)
from src.modules.courses.schemas import CoursePublic

# "admin" is a plain string: CurrentUser.role is a raw claim decoded from the JWT, and
# the role enum belongs to the identity service. Duplicating one stable claim value is
# the price of not sharing a business enum across services (CLAUDE.md §4).
_ADMIN_ROLE = "admin"


class CourseNotFoundError(NotFoundError):
    code = "COURSE_NOT_FOUND"
    message = "Course not found"


class InvalidCourseStateError(ConflictError):
    """The requested transition isn't allowed from the course's current status."""

    code = "INVALID_COURSE_STATE"


class CourseNotEditableError(ConflictError):
    code = "COURSE_NOT_EDITABLE"


def can_modify(course: Course, *, current_user_id: uuid.UUID, role: str) -> bool:
    return role == _ADMIN_ROLE or course.instructor_id == current_user_id


def ensure_can_modify(
    course: Course, *, current_user_id: uuid.UUID, role: str, action: str
) -> None:
    if not can_modify(course, current_user_id=current_user_id, role=role):
        raise ForbiddenError(f"Only the owning instructor or an admin can {action} this course")


def ensure_content_editable(course: Course) -> None:
    if course.status not in EDITABLE_CONTENT_STATUSES:
        hint = (
            "Unpublish it first."
            if course.status == CourseStatus.PUBLISHED
            else "Wait for the publish run to finish."
            if course.status == CourseStatus.PUBLISHING
            else "Archived courses are read-only."
        )
        raise CourseNotEditableError(
            f"Course content can only be changed in draft or failed status "
            f"(current: {course.status.value}). {hint}"
        )


async def create_course(
    session: AsyncSession,
    *,
    title: str,
    description: str,
    level: CourseLevel,
    instructor_id: uuid.UUID,
    max_enrollments: int | None = None,
) -> Course:
    course = Course(
        title=title,
        description=description,
        level=level,
        instructor_id=instructor_id,
        max_enrollments=max_enrollments,
        revision=0,
        status=CourseStatus.DRAFT,
    )
    session.add(course)
    await record_course_event(session, course, "course.created.v1")
    return course


async def get_course_or_404(session: AsyncSession, course_id: uuid.UUID) -> Course:
    course = await session.get(Course, course_id)
    if course is None:
        raise CourseNotFoundError()
    return course


async def get_course_for_update(session: AsyncSession, course_id: uuid.UUID) -> Course:
    """Row-locks the course for the rest of the transaction. Every write to the
    aggregate (metadata, status, modules, lessons) goes through this, so concurrent
    writers serialise on the course and ``revision`` never skips or repeats."""
    result = await session.execute(
        select(Course)
        .where(Course.id == course_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    course = result.scalar_one_or_none()
    if course is None:
        raise CourseNotFoundError()
    return course


async def list_courses(
    session: AsyncSession,
    *,
    q: str | None,
    level: CourseLevel | None,
    status: CourseStatus | None,
    limit: int,
    cursor: str | None,
) -> CursorPage[CoursePublic]:
    stmt = select(Course).order_by(Course.created_at.desc(), Course.id.desc())
    if q:
        stmt = stmt.where(Course.title.ilike(f"%{q}%"))
    if level is not None:
        stmt = stmt.where(Course.level == level)
    if status is not None:
        stmt = stmt.where(Course.status == status)
    if cursor:
        position = decode_cursor(cursor)
        cursor_created_at = datetime.fromisoformat(position["created_at"])
        cursor_id = uuid.UUID(position["id"])
        stmt = stmt.where(
            or_(
                Course.created_at < cursor_created_at,
                and_(Course.created_at == cursor_created_at, Course.id < cursor_id),
            )
        )

    result = await session.execute(stmt.limit(limit + 1))
    rows = list(result.scalars())
    has_more = len(rows) > limit
    rows = rows[:limit]

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = encode_cursor({"created_at": last.created_at.isoformat(), "id": str(last.id)})

    items = [CoursePublic.model_validate(row, from_attributes=True) for row in rows]
    return CursorPage(items=items, next_cursor=next_cursor, has_more=has_more)


async def update_course(
    session: AsyncSession,
    course_id: uuid.UUID,
    *,
    current_user_id: uuid.UUID,
    role: str,
    title: str | None,
    description: str | None,
    level: CourseLevel | None,
    # ``...`` = not provided (leave unchanged); ``None`` = remove the seat limit.
    max_enrollments: int | None | EllipsisType = ...,
) -> Course:
    course = await get_course_for_update(session, course_id)
    ensure_can_modify(course, current_user_id=current_user_id, role=role, action="update")
    # Metadata stays editable while published (title typo, more seats) — learning gets
    # the change through course.updated.v1. Not mid-publish, and never once archived.
    if course.status in (CourseStatus.PUBLISHING, CourseStatus.ARCHIVED):
        raise CourseNotEditableError(
            f"Course metadata can't be changed while {course.status.value}"
        )

    if title is not None:
        course.title = title
    if description is not None:
        course.description = description
    if level is not None:
        course.level = level
    if not isinstance(max_enrollments, EllipsisType):
        course.max_enrollments = max_enrollments

    await record_course_event(session, course, "course.updated.v1")
    return course


async def unpublish_course(
    session: AsyncSession, course_id: uuid.UUID, *, current_user_id: uuid.UUID, role: str
) -> Course:
    """Withdraw from the catalogue: ``published → draft``. Existing enrollments are
    untouched (learning owns them); new enrollments stop once learning applies
    course.unpublished.v1."""
    course = await get_course_for_update(session, course_id)
    ensure_can_modify(course, current_user_id=current_user_id, role=role, action="unpublish")
    if course.status != CourseStatus.PUBLISHED:
        raise InvalidCourseStateError(
            f"Only a published course can be unpublished (current: {course.status.value})"
        )
    course.status = CourseStatus.DRAFT
    await record_course_event(session, course, "course.unpublished.v1")
    return course


async def archive_course(
    session: AsyncSession, course_id: uuid.UUID, *, current_user_id: uuid.UUID, role: str
) -> Course:
    course = await get_course_for_update(session, course_id)
    ensure_can_modify(course, current_user_id=current_user_id, role=role, action="archive")
    if course.status == CourseStatus.ARCHIVED:
        return course  # DELETE is idempotent: archiving twice is a no-op, not an error.
    if course.status == CourseStatus.PUBLISHING:
        raise InvalidCourseStateError(
            "A course can't be archived while a publish run is in progress"
        )
    course.status = CourseStatus.ARCHIVED
    await record_course_event(session, course, "course.archived.v1")
    return course
