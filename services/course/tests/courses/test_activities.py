"""The activity functions PublishCourseWorkflow schedules, called directly — the same
code a Temporal worker runs, minus the transport. Requires Docker (testcontainers)."""

import uuid

import pytest
from temporalio.exceptions import ApplicationError

from src.db import postgres
from src.modules.courses import activities
from src.modules.courses.models import CourseStatus, PublishRunStatus
from src.modules.courses.service import courses, publishing
from tests.courses.helpers import INSTRUCTOR, make_course


async def _requested(owner: uuid.UUID, *, modules: int) -> tuple[uuid.UUID, uuid.UUID]:
    async with postgres.database.session_scope() as session:
        course = await make_course(session, owner, modules=modules)
        run = await publishing.request_publish(
            session, course.id, current_user_id=owner, role=INSTRUCTOR
        )
        return course.id, run.id


async def test_validate_then_mark_published(_db: None) -> None:
    course_id, run_id = await _requested(uuid.uuid4(), modules=1)
    await activities.validate_course(activities.PublishRunInput(run_id=run_id))
    await activities.mark_published(activities.PublishRunInput(run_id=run_id))

    async with postgres.database.session_scope() as session:
        assert (
            await courses.get_course_or_404(session, course_id)
        ).status == CourseStatus.PUBLISHED


async def test_validation_failure_is_non_retryable_and_keeps_the_running_step(_db: None) -> None:
    owner = uuid.uuid4()
    course_id, run_id = await _requested(owner, modules=0)

    with pytest.raises(ApplicationError) as raised:
        await activities.validate_course(activities.PublishRunInput(run_id=run_id))
    assert raised.value.type == activities.VALIDATION_FAILED
    assert raised.value.non_retryable

    async with postgres.database.session_scope() as session:
        run = await publishing.get_run(
            session, course_id, run_id, current_user_id=owner, role=INSTRUCTOR
        )
        assert run.status == PublishRunStatus.RUNNING  # committed before the raise

    await activities.mark_failed(
        activities.PublishFailureInput(run_id=run_id, failed_step="validate", error="empty")
    )
    async with postgres.database.session_scope() as session:
        assert (await courses.get_course_or_404(session, course_id)).status == CourseStatus.FAILED


@pytest.mark.parametrize("activity", ["validate_course", "mark_published", "mark_failed"])
async def test_an_unknown_run_is_a_non_retryable_failure(_db: None, activity: str) -> None:
    run_id = uuid.uuid4()
    data: object = (
        activities.PublishFailureInput(run_id=run_id, failed_step="validate", error="x")
        if activity == "mark_failed"
        else activities.PublishRunInput(run_id=run_id)
    )
    with pytest.raises(ApplicationError) as raised:
        await getattr(activities, activity)(data)
    assert raised.value.type == activities.RUN_NOT_FOUND
