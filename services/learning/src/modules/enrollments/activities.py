"""Temporal activities on ``learning-tq``. Discovered by the worker via ``ACTIVITIES``.

``apply_course_event`` is learning's half of the course → learning contract: course's
``DeliverCourseEventWorkflow`` schedules it by name (smartcourse_contracts), so the two
services share a payload schema and an activity name — no code, no HTTP client.
"""

import structlog
from temporalio import activity

from smartcourse_contracts.course_events import APPLY_COURSE_EVENT_ACTIVITY, CourseEventV1
from src.db.postgres import database
from src.modules.enrollments import service

logger = structlog.get_logger(__name__)


@activity.defn(name=APPLY_COURSE_EVENT_ACTIVITY)
async def apply_course_event(event: CourseEventV1) -> None:
    async with database.session_scope() as session:
        applied = await service.apply_course_event(session, event)
    await logger.ainfo(
        "course_event_applied" if applied else "course_event_skipped",
        event_id=str(event.event_id),
        event_type=event.event_type,
        course_id=str(event.course.course_id),
        revision=event.course.revision,
    )


ACTIVITIES = [apply_course_event]
