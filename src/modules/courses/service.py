"""Course catalogue business logic. Ownership checks (owning instructor or admin) live
here per CLAUDE.md §10 ("ownership checks live in the owning module's service"), not as
an inline role check in routes.py.

Every function takes the session as its first argument rather than fetching one, so the
same function is callable from a route, a task, or a consumer unchanged.
"""

import uuid
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import ForbiddenError, NotFoundError
from src.core.response import CursorPage, decode_cursor, encode_cursor
from src.modules.courses.models import Course, CourseLevel, CourseStatus
from src.modules.courses.schemas import CoursePublic

# "admin" is a plain string, not an import of users.models.UserRole: CurrentUser.role
# (core/dependencies.py) is already a raw string decoded from the JWT, and courses has
# no other reason to depend on the users module's model. Duplicating one stable claim
# value here is cheaper than the cross-module coupling CLAUDE.md §4 warns against.
_ADMIN_ROLE = "admin"


def _can_modify(course: Course, *, current_user_id: uuid.UUID, role: str) -> bool:
    return role == _ADMIN_ROLE or course.instructor_id == current_user_id


async def create_course(
    session: AsyncSession,
    *,
    title: str,
    description: str,
    level: CourseLevel,
    instructor_id: uuid.UUID,
) -> Course:
    course = Course(title=title, description=description, level=level, instructor_id=instructor_id)
    session.add(course)
    await session.flush()
    return course


async def get_course_or_404(session: AsyncSession, course_id: uuid.UUID) -> Course:
    course = await session.get(Course, course_id)
    if course is None:
        raise NotFoundError("Course not found")
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
) -> Course:
    course = await get_course_or_404(session, course_id)
    if not _can_modify(course, current_user_id=current_user_id, role=role):
        raise ForbiddenError("Only the owning instructor or an admin can update this course")

    if title is not None:
        course.title = title
    if description is not None:
        course.description = description
    if level is not None:
        course.level = level

    await session.flush()
    return course


async def archive_course(
    session: AsyncSession, course_id: uuid.UUID, *, current_user_id: uuid.UUID, role: str
) -> Course:
    course = await get_course_or_404(session, course_id)
    if not _can_modify(course, current_user_id=current_user_id, role=role):
        raise ForbiddenError("Only the owning instructor or an admin can archive this course")

    course.status = CourseStatus.ARCHIVED
    await session.flush()
    return course
