"""One-off: announce every existing enrollment's current state — ``enrollment.created``,
``.withdrawn`` or ``.completed`` — for enrollments that predate the event spine (week 3).
Analytics upserts by enrollment id; notification records a welcome only for an
enrollment from the last day. Run via `make backfill` (after course's, so course_cache
is current), with the learning worker running.
"""

import asyncio

from sqlalchemy import select

from smartcourse_contracts.enrollment_events import EnrollmentEventType
from src.config import get_settings
from src.db import postgres
from src.modules.enrollments.events import record_enrollment_event
from src.modules.enrollments.models import CourseCache, Enrollment, EnrollmentStatus

_EVENT: dict[EnrollmentStatus, EnrollmentEventType] = {
    EnrollmentStatus.ACTIVE: "enrollment.created.v1",
    EnrollmentStatus.WITHDRAWN: "enrollment.withdrawn.v1",
    EnrollmentStatus.COMPLETED: "enrollment.completed.v1",
}


async def main() -> None:
    postgres.init_postgres(get_settings().postgres)
    try:
        async with postgres.database.session_scope() as session:
            rows = list(
                (
                    await session.execute(
                        select(Enrollment, CourseCache)
                        .join(CourseCache, CourseCache.course_id == Enrollment.course_id)
                        .order_by(Enrollment.enrolled_at)
                    )
                ).tuples()
            )
            for enrollment, course in rows:
                await record_enrollment_event(
                    session, enrollment, course, _EVENT[enrollment.status]
                )
        print(f"learning: queued enrollment events for {len(rows)} enrollment(s)")
    finally:
        await postgres.close_postgres()


if __name__ == "__main__":
    asyncio.run(main())
