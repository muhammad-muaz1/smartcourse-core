"""Requires Docker (testcontainers spins up a real Postgres)."""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from platform_core.exceptions import ForbiddenError
from src.db import postgres
from src.modules.courses.models import Lesson, LessonType
from src.modules.courses.service import structure
from src.modules.courses.service.structure import InvalidOrderError
from tests.courses.helpers import INSTRUCTOR, make_course


class TestModules:
    async def test_positions_are_dense_through_add_reorder_and_delete(self, _db: None) -> None:
        owner = uuid.uuid4()
        async with postgres.database.session_scope() as session:
            course = await make_course(session, owner, modules=3)
            tree = await structure.module_tree(session, course.id)
            ids = [m.id for m in tree]
            assert [m.position for m in tree] == [0, 1, 2]

            reordered = await structure.reorder_modules(
                session, course.id, current_user_id=owner, role=INSTRUCTOR, module_ids=ids[::-1]
            )
            assert [m.id for m in reordered] == ids[::-1]

            await structure.delete_module(session, ids[1], current_user_id=owner, role=INSTRUCTOR)
            tree = await structure.module_tree(session, course.id)
            assert [m.id for m in tree] == [ids[2], ids[0]]
            assert [m.position for m in tree] == [0, 1]

    async def test_reorder_must_be_a_full_permutation(self, _db: None) -> None:
        owner = uuid.uuid4()
        async with postgres.database.session_scope() as session:
            course = await make_course(session, owner, modules=2)
            tree = await structure.module_tree(session, course.id)
            with pytest.raises(InvalidOrderError):
                await structure.reorder_modules(
                    session,
                    course.id,
                    current_user_id=owner,
                    role=INSTRUCTOR,
                    module_ids=[tree[0].id],
                )

    async def test_non_owner_cannot_edit_but_admin_can(self, _db: None) -> None:
        owner = uuid.uuid4()
        async with postgres.database.session_scope() as session:
            course = await make_course(session, owner)
            with pytest.raises(ForbiddenError):
                await structure.add_module(
                    session,
                    course.id,
                    current_user_id=uuid.uuid4(),
                    role=INSTRUCTOR,
                    title="x",
                    description=None,
                )
            module = await structure.add_module(
                session,
                course.id,
                current_user_id=uuid.uuid4(),
                role="admin",
                title="x",
                description=None,
            )
            assert module.position == 0


class TestLessons:
    async def test_unpublished_lessons_are_hidden_from_everyone_but_owner_and_admin(
        self, _db: None
    ) -> None:
        owner = uuid.uuid4()
        async with postgres.database.session_scope() as session:
            course = await make_course(session, owner, modules=1)
            lesson_id = (await structure.module_tree(session, course.id))[0].lessons[0].id
            assert (
                await structure.get_lesson(session, lesson_id, viewer_id=owner, role=INSTRUCTOR)
            ).id == lesson_id
            await structure.get_lesson(session, lesson_id, viewer_id=uuid.uuid4(), role="admin")
            with pytest.raises(structure.LessonNotFoundError):
                await structure.get_lesson(
                    session, lesson_id, viewer_id=uuid.uuid4(), role="student"
                )

    async def test_the_database_rejects_a_body_that_does_not_match_its_type(
        self, _db: None
    ) -> None:
        """The CHECK constraint is the backstop for anything that bypasses the schema."""
        owner = uuid.uuid4()
        async with postgres.database.session_scope() as session:
            course = await make_course(session, owner, modules=1)
            module_id = (await structure.module_tree(session, course.id))[0].id
            course_id = course.id

        with pytest.raises(IntegrityError, match="ck_lessons_body_matches_lesson_type"):
            async with postgres.database.session_scope() as session:
                session.add(
                    Lesson(
                        module_id=module_id,
                        course_id=course_id,
                        title="bad",
                        lesson_type=LessonType.VIDEO,
                        body={"content": "not a video"},
                        position=5,
                    )
                )
                await session.flush()

    async def test_body_alone_is_validated_against_the_current_type(self, _db: None) -> None:
        owner = uuid.uuid4()
        async with postgres.database.session_scope() as session:
            course = await make_course(session, owner, modules=1)
            lesson_id = (await structure.module_tree(session, course.id))[0].lessons[0].id
            with pytest.raises(ValueError):
                await structure.update_lesson(
                    session,
                    lesson_id,
                    current_user_id=owner,
                    role=INSTRUCTOR,
                    title=None,
                    lesson_type=None,
                    body={"video_url": "https://example.com/v.mp4"},
                    duration_minutes=None,
                )
