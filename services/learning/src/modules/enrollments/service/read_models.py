"""The two local read models, applied from events (CLAUDE.md §8). Called by this module's
consumers inside the consumer runner's transaction — after the inbox claim, so a
redelivered event never reaches these functions twice.
"""

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from smartcourse_contracts.course_events import CourseEventV1
from smartcourse_contracts.user_events import UserEventV1
from src.modules.enrollments.models import CourseCache, StudentDirectory
from src.modules.enrollments.service.enrollments import now

_STUDENT_ROLE = "student"


async def apply_course_event(session: AsyncSession, event: CourseEventV1) -> bool:
    """Upsert the course's snapshot. Returns False if the event is older than what's held
    — two different events delivered out of order. (The same event delivered twice is
    the inbox's job, one layer up.)"""
    snapshot = event.course
    values = {
        "title": snapshot.title,
        "status": snapshot.status,
        "instructor_id": snapshot.instructor_id,
        "max_enrollments": snapshot.max_enrollments,
        "published_at": snapshot.published_at,
        "revision": snapshot.revision,
        "synced_at": now(),
    }
    # A producer that predates lesson_ids sends None: keep what we have rather than
    # wiping the lesson list (and with it everyone's progress).
    if snapshot.lesson_ids is not None:
        values["lesson_ids"] = [str(lesson_id) for lesson_id in snapshot.lesson_ids]
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


async def apply_user_registered(session: AsyncSession, event: UserEventV1) -> bool:
    """Remember a student's display name for rosters. Only students enrol, and only the
    name is kept — the email in the event is personal data this service doesn't need."""
    user = event.user
    if user.role != _STUDENT_ROLE:
        return False
    values = {"full_name": user.full_name, "synced_at": now()}
    await session.execute(
        insert(StudentDirectory)
        .values(user_id=user.user_id, **values)
        .on_conflict_do_update(index_elements=[StudentDirectory.user_id], set_=values)
    )
    return True
