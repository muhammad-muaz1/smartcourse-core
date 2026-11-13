"""Publishing: the request that starts a run, the reads behind its status resource, and
the transitions the ``PublishCourseWorkflow`` activities perform (API design §6).

    POST /publish ──▶ course=publishing, run=pending, outbox(publish_requested)   [1 tx]
    relay         ──▶ starts PublishCourseWorkflow(run_id)
    validate      ──▶ run=running; checks the content tree; records the step       [1 tx]
    mark_published──▶ course=published + outbox(course.published.v1), run=succeeded [1 tx]
    on any failure──▶ course=failed  + outbox(course.updated.v1, workflow.failed.v1),
                      run=failed                                                   [1 tx]

Each workflow-side function is safe to run twice (activities are at-least-once): it
checks the run's status before acting and returns quietly if the work is already done.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.exceptions import NotFoundError
from src.modules.courses.events import (
    record_course_event,
    record_publish_failed,
    record_publish_requested,
)
from src.modules.courses.models import (
    PUBLISHABLE_STATUSES,
    Course,
    CourseModule,
    CourseStatus,
    Lesson,
    PublishRun,
    PublishRunStatus,
)
from src.modules.courses.service.courses import (
    InvalidCourseStateError,
    ensure_can_modify,
    get_course_for_update,
    get_course_or_404,
)

STEP_VALIDATE = "validate"
STEP_MARK_PUBLISHED = "mark_published"
# Content extraction, chunking and search indexing are activities on content-tq in the
# design; they slot in between these two once the content service exists (docs/adr/0011).
PUBLISH_STEPS = (STEP_VALIDATE, STEP_MARK_PUBLISHED)

_FINISHED = frozenset({PublishRunStatus.SUCCEEDED, PublishRunStatus.FAILED})


class PublishRunNotFoundError(NotFoundError):
    code = "PUBLISH_RUN_NOT_FOUND"
    message = "Publish run not found"


def workflow_id_for(run_id: uuid.UUID) -> str:
    return f"publish-run-{run_id}"


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _set_step(run: PublishRun, name: str, status: str) -> None:
    """jsonb is replaced, not mutated in place — SQLAlchemy doesn't track in-place
    changes to a JSON value."""
    now = _now().isoformat()
    steps: list[dict[str, Any]] = []
    for step in run.steps:
        step = dict(step)
        if step["name"] == name:
            step["status"] = status
            if status == "running":
                step["started_at"] = now
            elif status in ("succeeded", "failed"):
                step["finished_at"] = now
                step.setdefault("started_at", now)
        steps.append(step)
    run.steps = steps


# --- API side ----------------------------------------------------------------------


async def request_publish(
    session: AsyncSession, course_id: uuid.UUID, *, current_user_id: uuid.UUID, role: str
) -> PublishRun:
    course = await get_course_for_update(session, course_id)
    ensure_can_modify(course, current_user_id=current_user_id, role=role, action="publish")
    if course.status not in PUBLISHABLE_STATUSES:
        raise InvalidCourseStateError(
            f"Only a draft or failed course can be published (current: {course.status.value})"
        )

    run_id = uuid.uuid4()
    run = PublishRun(
        id=run_id,
        course_id=course.id,
        requested_by=current_user_id,
        status=PublishRunStatus.PENDING,
        workflow_id=workflow_id_for(run_id),
        steps=[{"name": name, "status": "pending"} for name in PUBLISH_STEPS],
    )
    session.add(run)
    course.status = CourseStatus.PUBLISHING
    await record_course_event(session, course, "course.updated.v1")
    record_publish_requested(session, run)
    await session.flush()
    return run


async def list_runs(
    session: AsyncSession,
    course_id: uuid.UUID,
    *,
    current_user_id: uuid.UUID,
    role: str,
    limit: int,
) -> list[PublishRun]:
    course = await get_course_or_404(session, course_id)
    ensure_can_modify(course, current_user_id=current_user_id, role=role, action="view runs of")
    result = await session.execute(
        select(PublishRun)
        .where(PublishRun.course_id == course_id)
        .order_by(PublishRun.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars())


async def get_run(
    session: AsyncSession,
    course_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    current_user_id: uuid.UUID,
    role: str,
) -> PublishRun:
    run = await session.get(PublishRun, run_id)
    if run is None or run.course_id != course_id:
        raise PublishRunNotFoundError()
    course = await session.get(Course, course_id)
    if course is None:
        raise PublishRunNotFoundError()
    ensure_can_modify(course, current_user_id=current_user_id, role=role, action="view runs of")
    return run


# --- workflow side -----------------------------------------------------------------


async def _run_for_update(session: AsyncSession, run_id: uuid.UUID) -> PublishRun:
    result = await session.execute(
        select(PublishRun).where(PublishRun.id == run_id).with_for_update()
    )
    run = result.scalar_one_or_none()
    if run is None:
        raise PublishRunNotFoundError()
    return run


async def validate_for_publish(session: AsyncSession, run_id: uuid.UUID) -> list[str]:
    """Marks the run running and checks the content tree. Returns the problems found
    (empty = publishable); the caller turns a non-empty list into a step failure. The
    step's progress is recorded either way — this transaction commits."""
    run = await _run_for_update(session, run_id)
    if run.status in _FINISHED:
        return []
    course = await get_course_for_update(session, run.course_id)

    run.status = PublishRunStatus.RUNNING
    run.started_at = run.started_at or _now()
    _set_step(run, STEP_VALIDATE, "running")

    problems: list[str] = []
    if course.status != CourseStatus.PUBLISHING:
        problems.append(f"course is {course.status.value}, not publishing")

    modules = (
        await session.execute(
            select(CourseModule.id, CourseModule.title)
            .where(CourseModule.course_id == course.id)
            .order_by(CourseModule.position)
        )
    ).all()
    if not modules:
        problems.append("course has no modules")

    lesson_rows = await session.execute(
        select(Lesson.module_id, func.count())
        .where(Lesson.course_id == course.id)
        .group_by(Lesson.module_id)
    )
    lesson_counts: dict[uuid.UUID, int] = {row[0]: row[1] for row in lesson_rows.all()}
    problems.extend(
        f"module '{title}' has no lessons"
        for module_id, title in modules
        if not lesson_counts.get(module_id)
    )

    if not problems:
        _set_step(run, STEP_VALIDATE, "succeeded")
    await session.flush()
    return problems


async def complete_publish(session: AsyncSession, run_id: uuid.UUID) -> None:
    run = await _run_for_update(session, run_id)
    if run.status in _FINISHED:
        return
    course = await get_course_for_update(session, run.course_id)

    _set_step(run, STEP_MARK_PUBLISHED, "running")
    course.status = CourseStatus.PUBLISHED
    course.published_at = _now()
    await record_course_event(session, course, "course.published.v1")

    _set_step(run, STEP_MARK_PUBLISHED, "succeeded")
    run.status = PublishRunStatus.SUCCEEDED
    run.finished_at = _now()
    await session.flush()


async def fail_publish(
    session: AsyncSession, run_id: uuid.UUID, *, failed_step: str, error: str
) -> None:
    """Compensation: the run is failed with the failing step recorded, and the course
    leaves ``publishing`` for ``failed`` so the instructor can fix it and publish again.
    There are no partial artefacts to remove yet — once content steps exist, their
    compensations (delete partial chunks) run before this."""
    run = await _run_for_update(session, run_id)
    if run.status in _FINISHED:
        return
    course = await get_course_for_update(session, run.course_id)

    _set_step(run, failed_step, "failed")
    run.status = PublishRunStatus.FAILED
    run.failed_step = failed_step
    run.error = error[:2000]
    run.finished_at = _now()

    if course.status == CourseStatus.PUBLISHING:
        course.status = CourseStatus.FAILED
        await record_course_event(session, course, "course.updated.v1")
    # Analytics' "workflow issues" metric, in the same transaction as the failure.
    record_publish_failed(session, run)
    await session.flush()
