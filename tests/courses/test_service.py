"""Requires Docker (testcontainers spins up a real Postgres)."""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from testcontainers.community.postgres import PostgresContainer

from src.config import PostgresSettings
from src.core.exceptions import ForbiddenError, NotFoundError
from src.db import postgres
from src.db.base import Base
from src.modules.courses import service
from src.modules.courses.models import CourseLevel, CourseStatus
from src.modules.users import service as users_service
from src.modules.users.models import UserRole


@pytest.fixture
async def _db(postgres_container: PostgresContainer) -> AsyncIterator[None]:
    postgres.init_postgres(PostgresSettings(dsn=postgres_container.get_connection_url()))
    engine = postgres._engine  # noqa: SLF001 - test-only setup/teardown
    assert engine is not None
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await postgres.close_postgres()


async def _make_user(session: AsyncSession, *, role: str = "instructor") -> uuid.UUID:
    user = await users_service.create_user(
        session,
        email=f"{uuid.uuid4()}@example.com",
        hashed_password="hashed",
        full_name="Test User",
        role=UserRole(role),
    )
    return user.id


class TestCreateCourse:
    async def test_create_course_then_get_by_id(self, _db: None) -> None:
        async for session in postgres.get_session():
            instructor_id = await _make_user(session)
            course = await service.create_course(
                session,
                title="Intro to Python",
                description="Learn Python basics",
                level=CourseLevel.BEGINNER,
                instructor_id=instructor_id,
            )
            course_id = course.id

        async for session in postgres.get_session():
            found = await service.get_course_or_404(session, course_id)
            assert found.title == "Intro to Python"
            assert found.status == CourseStatus.DRAFT
            assert found.instructor_id == instructor_id


class TestGetCourse:
    async def test_get_course_or_404_raises_when_missing(self, _db: None) -> None:
        async for session in postgres.get_session():
            with pytest.raises(NotFoundError):
                await service.get_course_or_404(session, uuid.uuid4())


class TestListCourses:
    async def test_filters_by_title_and_level(self, _db: None) -> None:
        async for session in postgres.get_session():
            instructor_id = await _make_user(session)
            await service.create_course(
                session,
                title="Intro to Python",
                description="d",
                level=CourseLevel.BEGINNER,
                instructor_id=instructor_id,
            )
            await service.create_course(
                session,
                title="Advanced Rust",
                description="d",
                level=CourseLevel.ADVANCED,
                instructor_id=instructor_id,
            )

        async for session in postgres.get_session():
            page = await service.list_courses(
                session, q="Python", level=None, status=None, limit=20, cursor=None
            )
            assert [item.title for item in page.items] == ["Intro to Python"]

            page = await service.list_courses(
                session, q=None, level=CourseLevel.ADVANCED, status=None, limit=20, cursor=None
            )
            assert [item.title for item in page.items] == ["Advanced Rust"]

    async def test_paginates_with_cursor(self, _db: None) -> None:
        async for session in postgres.get_session():
            instructor_id = await _make_user(session)
            for i in range(3):
                await service.create_course(
                    session,
                    title=f"Course {i}",
                    description="d",
                    level=CourseLevel.BEGINNER,
                    instructor_id=instructor_id,
                )

        async for session in postgres.get_session():
            first_page = await service.list_courses(
                session, q=None, level=None, status=None, limit=2, cursor=None
            )
            assert len(first_page.items) == 2
            assert first_page.has_more is True
            assert first_page.next_cursor is not None

            second_page = await service.list_courses(
                session,
                q=None,
                level=None,
                status=None,
                limit=2,
                cursor=first_page.next_cursor,
            )
            assert len(second_page.items) == 1
            assert second_page.has_more is False
            seen_ids = {item.id for item in first_page.items} | {
                item.id for item in second_page.items
            }
            assert len(seen_ids) == 3


class TestUpdateCourse:
    async def test_owner_can_update(self, _db: None) -> None:
        async for session in postgres.get_session():
            instructor_id = await _make_user(session)
            course = await service.create_course(
                session,
                title="Original",
                description="d",
                level=CourseLevel.BEGINNER,
                instructor_id=instructor_id,
            )
            course_id = course.id

        async for session in postgres.get_session():
            updated = await service.update_course(
                session,
                course_id,
                current_user_id=instructor_id,
                role="instructor",
                title="Updated title",
                description=None,
                level=None,
            )
            assert updated.title == "Updated title"
            assert updated.description == "d"

    async def test_non_owner_instructor_is_forbidden(self, _db: None) -> None:
        async for session in postgres.get_session():
            owner_id = await _make_user(session)
            other_id = await _make_user(session)
            course = await service.create_course(
                session,
                title="Original",
                description="d",
                level=CourseLevel.BEGINNER,
                instructor_id=owner_id,
            )
            course_id = course.id

        async for session in postgres.get_session():
            with pytest.raises(ForbiddenError):
                await service.update_course(
                    session,
                    course_id,
                    current_user_id=other_id,
                    role="instructor",
                    title="Hijacked",
                    description=None,
                    level=None,
                )

    async def test_admin_can_update_any_course(self, _db: None) -> None:
        async for session in postgres.get_session():
            owner_id = await _make_user(session)
            admin_id = await _make_user(session, role="admin")
            course = await service.create_course(
                session,
                title="Original",
                description="d",
                level=CourseLevel.BEGINNER,
                instructor_id=owner_id,
            )
            course_id = course.id

        async for session in postgres.get_session():
            updated = await service.update_course(
                session,
                course_id,
                current_user_id=admin_id,
                role="admin",
                title="Admin edited",
                description=None,
                level=None,
            )
            assert updated.title == "Admin edited"


class TestArchiveCourse:
    async def test_owner_can_archive(self, _db: None) -> None:
        async for session in postgres.get_session():
            instructor_id = await _make_user(session)
            course = await service.create_course(
                session,
                title="Original",
                description="d",
                level=CourseLevel.BEGINNER,
                instructor_id=instructor_id,
            )
            course_id = course.id

        async for session in postgres.get_session():
            archived = await service.archive_course(
                session, course_id, current_user_id=instructor_id, role="instructor"
            )
            assert archived.status == CourseStatus.ARCHIVED

    async def test_non_owner_instructor_is_forbidden(self, _db: None) -> None:
        async for session in postgres.get_session():
            owner_id = await _make_user(session)
            other_id = await _make_user(session)
            course = await service.create_course(
                session,
                title="Original",
                description="d",
                level=CourseLevel.BEGINNER,
                instructor_id=owner_id,
            )
            course_id = course.id

        async for session in postgres.get_session():
            with pytest.raises(ForbiddenError):
                await service.archive_course(
                    session, course_id, current_user_id=other_id, role="instructor"
                )
