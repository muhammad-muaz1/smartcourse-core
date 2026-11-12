"""Requires Docker (testcontainers spins up a real Postgres). The workflow-side
functions are what PublishCourseWorkflow's activities call, one transaction each."""

import uuid

import pytest
from sqlalchemy import select

from platform_core.exceptions import ForbiddenError
from src.db import postgres
from src.db.outbox import OutboxMessage
from src.modules.courses.events import PUBLISH_REQUESTED_TOPIC
from src.modules.courses.models import CourseStatus, PublishRunStatus
from src.modules.courses.service import courses, publishing
from src.modules.courses.service.courses import CourseNotEditableError, InvalidCourseStateError
from src.modules.courses.service.structure import add_module
from tests.courses.helpers import INSTRUCTOR, make_course


async def _topics(session: object) -> list[str]:
    result = await session.execute(  # type: ignore[attr-defined]
        select(OutboxMessage.topic).order_by(OutboxMessage.created_at)
    )
    return list(result.scalars())


class TestRequestPublish:
    async def test_moves_course_to_publishing_and_writes_run_and_outbox(self, _db: None) -> None:
        owner = uuid.uuid4()
        async with postgres.database.session_scope() as session:
            course = await make_course(session, owner, modules=1)
            run = await publishing.request_publish(
                session, course.id, current_user_id=owner, role=INSTRUCTOR
            )
            run_id, course_id = run.id, course.id

        async with postgres.database.session_scope() as session:
            course = await courses.get_course_or_404(session, course_id)
            assert course.status == CourseStatus.PUBLISHING
            run = await publishing.get_run(
                session, course_id, run_id, current_user_id=owner, role=INSTRUCTOR
            )
            assert run.status == PublishRunStatus.PENDING
            assert run.workflow_id == f"publish-run-{run_id}"
            assert [s["name"] for s in run.steps] == ["validate", "mark_published"]
            topics = await _topics(session)
            assert topics[-2:] == ["smartcourse.course.updated.v1", PUBLISH_REQUESTED_TOPIC]

    async def test_only_draft_or_failed_can_be_published(self, _db: None) -> None:
        owner = uuid.uuid4()
        async with postgres.database.session_scope() as session:
            course = await make_course(session, owner, modules=1)
            await publishing.request_publish(
                session, course.id, current_user_id=owner, role=INSTRUCTOR
            )
            with pytest.raises(InvalidCourseStateError):
                await publishing.request_publish(
                    session, course.id, current_user_id=owner, role=INSTRUCTOR
                )

    async def test_non_owner_cannot_publish(self, _db: None) -> None:
        async with postgres.database.session_scope() as session:
            course = await make_course(session, uuid.uuid4(), modules=1)
            with pytest.raises(ForbiddenError):
                await publishing.request_publish(
                    session, course.id, current_user_id=uuid.uuid4(), role=INSTRUCTOR
                )


class TestPublishSaga:
    async def _requested(self, owner: uuid.UUID, *, modules: int) -> tuple[uuid.UUID, uuid.UUID]:
        async with postgres.database.session_scope() as session:
            course = await make_course(session, owner, modules=modules)
            run = await publishing.request_publish(
                session, course.id, current_user_id=owner, role=INSTRUCTOR
            )
            return course.id, run.id

    async def test_happy_path_publishes_and_emits_course_published(self, _db: None) -> None:
        owner = uuid.uuid4()
        course_id, run_id = await self._requested(owner, modules=2)

        async with postgres.database.session_scope() as session:
            assert await publishing.validate_for_publish(session, run_id) == []
        async with postgres.database.session_scope() as session:
            await publishing.complete_publish(session, run_id)

        async with postgres.database.session_scope() as session:
            course = await courses.get_course_or_404(session, course_id)
            run = await publishing.get_run(
                session, course_id, run_id, current_user_id=owner, role=INSTRUCTOR
            )
            assert course.status == CourseStatus.PUBLISHED
            assert course.published_at is not None
            assert run.status == PublishRunStatus.SUCCEEDED
            assert [s["status"] for s in run.steps] == ["succeeded", "succeeded"]
            assert (await _topics(session))[-1] == "smartcourse.course.published.v1"

    async def test_an_empty_course_fails_validation_and_compensates_to_failed(
        self, _db: None
    ) -> None:
        owner = uuid.uuid4()
        course_id, run_id = await self._requested(owner, modules=0)

        async with postgres.database.session_scope() as session:
            problems = await publishing.validate_for_publish(session, run_id)
        assert problems == ["course has no modules"]
        async with postgres.database.session_scope() as session:
            await publishing.fail_publish(
                session, run_id, failed_step="validate", error="; ".join(problems)
            )

        async with postgres.database.session_scope() as session:
            course = await courses.get_course_or_404(session, course_id)
            run = await publishing.get_run(
                session, course_id, run_id, current_user_id=owner, role=INSTRUCTOR
            )
            assert course.status == CourseStatus.FAILED
            assert run.status == PublishRunStatus.FAILED
            assert run.failed_step == "validate"
            assert run.error == "course has no modules"
            # failed is editable again, and publishable again
            await add_module(
                session,
                course_id,
                current_user_id=owner,
                role=INSTRUCTOR,
                title="M",
                description=None,
            )

    async def test_a_module_without_lessons_is_reported_by_title(self, _db: None) -> None:
        owner = uuid.uuid4()
        async with postgres.database.session_scope() as session:
            course = await make_course(session, owner, modules=1)
            await add_module(
                session,
                course.id,
                current_user_id=owner,
                role=INSTRUCTOR,
                title="Empty",
                description=None,
            )
            run = await publishing.request_publish(
                session, course.id, current_user_id=owner, role=INSTRUCTOR
            )
            run_id = run.id
        async with postgres.database.session_scope() as session:
            assert await publishing.validate_for_publish(session, run_id) == [
                "module 'Empty' has no lessons"
            ]

    async def test_workflow_steps_are_safe_to_run_twice(self, _db: None) -> None:
        """Activities are at-least-once: a redelivered step must not double-apply."""
        owner = uuid.uuid4()
        course_id, run_id = await self._requested(owner, modules=1)
        for _ in range(2):
            async with postgres.database.session_scope() as session:
                await publishing.validate_for_publish(session, run_id)
        for _ in range(2):
            async with postgres.database.session_scope() as session:
                await publishing.complete_publish(session, run_id)
        async with postgres.database.session_scope() as session:
            await publishing.fail_publish(session, run_id, failed_step="validate", error="late")

        async with postgres.database.session_scope() as session:
            course = await courses.get_course_or_404(session, course_id)
            assert course.status == CourseStatus.PUBLISHED
            topics = await _topics(session)
            assert topics.count("smartcourse.course.published.v1") == 1


class TestLifecycle:
    async def test_published_content_is_locked_until_unpublished(self, _db: None) -> None:
        owner = uuid.uuid4()
        async with postgres.database.session_scope() as session:
            course = await make_course(session, owner, modules=1)
            run = await publishing.request_publish(
                session, course.id, current_user_id=owner, role=INSTRUCTOR
            )
            course_id, run_id = course.id, run.id
        async with postgres.database.session_scope() as session:
            await publishing.complete_publish(session, run_id)

        async with postgres.database.session_scope() as session:
            with pytest.raises(CourseNotEditableError):
                await add_module(
                    session,
                    course_id,
                    current_user_id=owner,
                    role=INSTRUCTOR,
                    title="x",
                    description=None,
                )
        async with postgres.database.session_scope() as session:
            course = await courses.unpublish_course(
                session, course_id, current_user_id=owner, role=INSTRUCTOR
            )
            assert course.status == CourseStatus.DRAFT
            await add_module(
                session,
                course_id,
                current_user_id=owner,
                role=INSTRUCTOR,
                title="x",
                description=None,
            )

    async def test_every_change_bumps_the_revision(self, _db: None) -> None:
        owner = uuid.uuid4()
        async with postgres.database.session_scope() as session:
            course = await make_course(session, owner)
            assert course.revision == 1
            course = await courses.update_course(
                session,
                course.id,
                current_user_id=owner,
                role=INSTRUCTOR,
                title="New",
                description=None,
                level=None,
                max_enrollments=None,
            )
            assert course.revision == 2
            assert course.max_enrollments is None
            course = await courses.archive_course(
                session, course.id, current_user_id=owner, role=INSTRUCTOR
            )
            assert course.revision == 3
            assert await _topics(session) == [
                "smartcourse.course.created.v1",
                "smartcourse.course.updated.v1",
                "smartcourse.course.archived.v1",
            ]

    async def test_cannot_archive_mid_publish(self, _db: None) -> None:
        owner = uuid.uuid4()
        async with postgres.database.session_scope() as session:
            course = await make_course(session, owner, modules=1)
            await publishing.request_publish(
                session, course.id, current_user_id=owner, role=INSTRUCTOR
            )
            with pytest.raises(InvalidCourseStateError):
                await courses.archive_course(
                    session, course.id, current_user_id=owner, role=INSTRUCTOR
                )
