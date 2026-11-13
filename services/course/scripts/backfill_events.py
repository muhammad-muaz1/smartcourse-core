"""One-off: re-announce every course's current snapshot as ``course.updated.v1`` — for
courses that predate the event spine (week 3) or the ``lesson_ids`` field. Bumps each
course's revision, exactly like any other change, so learning's ``course_cache`` takes the
new snapshot (with the lesson list progress is measured against) and analytics learns
the course exists. Run via `make backfill`, with the course worker running.
"""

import asyncio

from sqlalchemy import select

from src.config import get_settings
from src.db import postgres
from src.modules.courses.events import record_course_event
from src.modules.courses.models import Course
from src.modules.courses.service.courses import get_course_for_update


async def main() -> None:
    postgres.init_postgres(get_settings().postgres)
    try:
        async with postgres.database.session_scope() as session:
            ids = list(
                (await session.execute(select(Course.id).order_by(Course.created_at))).scalars()
            )
            for course_id in ids:
                # The same row lock every writer takes, so revision can't collide.
                course = await get_course_for_update(session, course_id)
                await record_course_event(session, course, "course.updated.v1")
        print(f"course: queued course.updated.v1 for {len(ids)} course(s)")
    finally:
        await postgres.close_postgres()


if __name__ == "__main__":
    asyncio.run(main())
