"""Requires Docker (testcontainers spins up a real Postgres)."""

import uuid

import pytest

from platform_core.exceptions import ForbiddenError
from src.db import postgres
from src.modules.enrollments import service
from src.modules.enrollments.activities import apply_course_event
from src.modules.enrollments.models import CourseCache, EnrollmentStatus
from tests.enrollments.helpers import course_event


async def _course(**kwargs: object) -> uuid.UUID:
    course_id = uuid.uuid4()
    await apply_course_event(course_event(course_id, revision=1, **kwargs))  # type: ignore[arg-type]
    return course_id


async def _cache(course_id: uuid.UUID) -> CourseCache:
    async with postgres.database.session_scope() as session:
        cache = await session.get(CourseCache, course_id)
        assert cache is not None
        return cache


class TestCourseCache:
    async def test_a_duplicate_delivery_is_skipped(self, _db: None) -> None:
        event = course_event(uuid.uuid4(), revision=1)
        async with postgres.database.session_scope() as session:
            assert await service.apply_course_event(session, event) is True
        async with postgres.database.session_scope() as session:
            assert await service.apply_course_event(session, event) is False

    async def test_an_older_revision_never_overwrites_a_newer_one(self, _db: None) -> None:
        course_id = uuid.uuid4()
        await apply_course_event(course_event(course_id, revision=5, status="archived"))
        async with postgres.database.session_scope() as session:
            applied = await service.apply_course_event(
                session, course_event(course_id, revision=4, status="published")
            )
        assert applied is False
        cache = await _cache(course_id)
        assert (cache.status, cache.revision) == ("archived", 5)

    async def test_an_update_never_resets_the_seat_counter(self, _db: None) -> None:
        course_id = await _course()
        async with postgres.database.session_scope() as session:
            await service.enroll(session, course_id=course_id, student_id=uuid.uuid4())
        await apply_course_event(course_event(course_id, revision=2, title="Renamed"))
        cache = await _cache(course_id)
        assert (cache.title, cache.active_enrollments) == ("Renamed", 1)


class TestEnroll:
    async def test_unknown_course(self, _db: None) -> None:
        async with postgres.database.session_scope() as session:
            with pytest.raises(service.CourseNotFoundError):
                await service.enroll(session, course_id=uuid.uuid4(), student_id=uuid.uuid4())

    @pytest.mark.parametrize("status", ["draft", "publishing", "failed", "archived"])
    async def test_only_published_courses_are_enrollable(self, _db: None, status: str) -> None:
        course_id = await _course(status=status)
        async with postgres.database.session_scope() as session:
            with pytest.raises(service.CourseNotEnrollableError):
                await service.enroll(session, course_id=course_id, student_id=uuid.uuid4())

    async def test_duplicate_is_rejected_by_the_unique_index(self, _db: None) -> None:
        course_id = await _course()
        student = uuid.uuid4()
        async with postgres.database.session_scope() as session:
            await service.enroll(session, course_id=course_id, student_id=student)
        with pytest.raises(service.AlreadyEnrolledError):
            async with postgres.database.session_scope() as session:
                await service.enroll(session, course_id=course_id, student_id=student)
        assert (await _cache(course_id)).active_enrollments == 1

    async def test_seat_limit_and_duplicate_wins_over_full(self, _db: None) -> None:
        course_id = await _course(max_enrollments=1)
        first = uuid.uuid4()
        async with postgres.database.session_scope() as session:
            await service.enroll(session, course_id=course_id, student_id=first)
        with pytest.raises(service.CourseFullError):
            async with postgres.database.session_scope() as session:
                await service.enroll(session, course_id=course_id, student_id=uuid.uuid4())
        # An enrolled student retrying hears "already enrolled", not "full".
        with pytest.raises(service.AlreadyEnrolledError):
            async with postgres.database.session_scope() as session:
                await service.enroll(session, course_id=course_id, student_id=first)
        assert (await _cache(course_id)).active_enrollments == 1

    async def test_withdraw_frees_the_seat_and_allows_re_enrolment(self, _db: None) -> None:
        course_id = await _course(max_enrollments=1)
        student = uuid.uuid4()
        async with postgres.database.session_scope() as session:
            enrollment = await service.enroll(session, course_id=course_id, student_id=student)
            enrollment_id = enrollment.id
        async with postgres.database.session_scope() as session:
            withdrawn = await service.withdraw(
                session, enrollment_id, current_user_id=student, role="student"
            )
            assert withdrawn.status == EnrollmentStatus.WITHDRAWN
        async with postgres.database.session_scope() as session:  # idempotent
            await service.withdraw(session, enrollment_id, current_user_id=student, role="student")
        assert (await _cache(course_id)).active_enrollments == 0

        async with postgres.database.session_scope() as session:
            await service.enroll(session, course_id=course_id, student_id=student)
        async with postgres.database.session_scope() as session:
            history = await service.enrollment_history(
                session, student, current_user_id=student, role="student", limit=10, cursor=None
            )
        assert sorted(item.status for item in history.items) == ["active", "withdrawn"]

    async def test_nobody_else_can_withdraw_a_students_enrollment(self, _db: None) -> None:
        course_id = await _course()
        async with postgres.database.session_scope() as session:
            enrollment = await service.enroll(session, course_id=course_id, student_id=uuid.uuid4())
            with pytest.raises(service.EnrollmentNotFoundError):
                await service.withdraw(
                    session, enrollment.id, current_user_id=uuid.uuid4(), role="student"
                )


class TestReads:
    async def test_roster_is_for_the_courses_instructor_and_admins(self, _db: None) -> None:
        instructor = uuid.uuid4()
        course_id = await _course(instructor_id=instructor)
        async with postgres.database.session_scope() as session:
            for _ in range(3):
                await service.enroll(session, course_id=course_id, student_id=uuid.uuid4())
        async with postgres.database.session_scope() as session:
            page = await service.course_roster(
                session,
                course_id,
                current_user_id=instructor,
                role="instructor",
                status=None,
                limit=2,
                cursor=None,
            )
            assert len(page.items) == 2 and page.has_more
            rest = await service.course_roster(
                session,
                course_id,
                current_user_id=uuid.uuid4(),
                role="admin",
                status=EnrollmentStatus.ACTIVE,
                limit=2,
                cursor=page.next_cursor,
            )
            assert len(rest.items) == 1 and not rest.has_more
            with pytest.raises(ForbiddenError):
                await service.course_roster(
                    session,
                    course_id,
                    current_user_id=uuid.uuid4(),
                    role="instructor",
                    status=None,
                    limit=2,
                    cursor=None,
                )

    async def test_an_enrollment_is_visible_to_student_instructor_and_admin_only(
        self, _db: None
    ) -> None:
        instructor, student = uuid.uuid4(), uuid.uuid4()
        course_id = await _course(instructor_id=instructor)
        async with postgres.database.session_scope() as session:
            enrollment = await service.enroll(session, course_id=course_id, student_id=student)
            for viewer, role in (
                (student, "student"),
                (instructor, "instructor"),
                (uuid.uuid4(), "admin"),
            ):
                found = await service.get_enrollment(
                    session, enrollment.id, current_user_id=viewer, role=role
                )
                assert found.course_title == "Distributed Systems"
            with pytest.raises(service.EnrollmentNotFoundError):
                await service.get_enrollment(
                    session, enrollment.id, current_user_id=uuid.uuid4(), role="student"
                )
            with pytest.raises(ForbiddenError):
                await service.enrollment_history(
                    session,
                    student,
                    current_user_id=uuid.uuid4(),
                    role="student",
                    limit=5,
                    cursor=None,
                )
