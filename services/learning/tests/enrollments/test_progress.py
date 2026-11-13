"""Progress, completion, the enrollment.* outbox rows, and the roster name snapshot.
Requires Docker (testcontainers) or SMARTCOURSE_TEST_POSTGRES_URL."""

import uuid

import pytest
from sqlalchemy import select

from smartcourse_contracts.enrollment_events import EnrollmentEventV1
from src.db import postgres
from src.db.outbox import OutboxMessage
from src.modules.enrollments.models import EnrollmentStatus
from src.modules.enrollments.schemas import ProgressPublic
from src.modules.enrollments.service import enrollments, progress
from tests.enrollments.helpers import (
    apply_course_event,
    apply_user_event,
    course_event,
    user_event,
)


async def _course(lessons: int) -> tuple[uuid.UUID, list[uuid.UUID]]:
    course_id, lesson_ids = uuid.uuid4(), [uuid.uuid4() for _ in range(lessons)]
    await apply_course_event(course_event(course_id, revision=1, lesson_ids=lesson_ids))
    return course_id, lesson_ids


async def _enrol(course_id: uuid.UUID, student_id: uuid.UUID) -> uuid.UUID:
    async with postgres.database.session_scope() as session:
        enrollment = await enrollments.enroll(session, course_id=course_id, student_id=student_id)
        return enrollment.id


async def _events() -> list[EnrollmentEventV1]:
    async with postgres.database.session_scope() as session:
        rows = await session.execute(select(OutboxMessage).order_by(OutboxMessage.created_at))
        return [EnrollmentEventV1.model_validate(row.payload) for row in rows.scalars()]


async def _complete(
    enrollment_id: uuid.UUID, lesson_id: uuid.UUID, student_id: uuid.UUID
) -> ProgressPublic:
    async with postgres.database.session_scope() as session:
        return await progress.complete_lesson(
            session, enrollment_id, lesson_id, current_user_id=student_id
        )


async def test_enrolling_initialises_progress_and_announces_it(_db: None) -> None:
    course_id, _ = await _course(lessons=3)
    student = uuid.uuid4()
    enrollment_id = await _enrol(course_id, student)

    async with postgres.database.session_scope() as session:
        view = await progress.get_progress(
            session, enrollment_id, current_user_id=student, role="student"
        )
    assert (view.lessons_completed, view.lessons_total, view.progress_percent) == (0, 3, 0.0)

    [created] = await _events()
    assert created.event_type == "enrollment.created.v1"
    assert created.enrollment.enrollment_id == enrollment_id
    assert created.enrollment.lessons_total == 3


async def test_the_last_lesson_completes_the_enrollment_exactly_once(_db: None) -> None:
    course_id, lessons = await _course(lessons=2)
    student = uuid.uuid4()
    enrollment_id = await _enrol(course_id, student)

    first = await _complete(enrollment_id, lessons[0], student)
    again = await _complete(enrollment_id, lessons[0], student)  # a repeat is a no-op
    assert (first.lessons_completed, again.lessons_completed) == (1, 1)
    assert first.status == EnrollmentStatus.ACTIVE

    done = await _complete(enrollment_id, lessons[1], student)
    assert (done.status, done.progress_percent) == (EnrollmentStatus.COMPLETED, 100.0)
    assert done.completed_lesson_ids == lessons
    await _complete(enrollment_id, lessons[1], student)  # completing again emits nothing

    types = [event.event_type for event in await _events()]
    assert types == ["enrollment.created.v1", "enrollment.completed.v1"]


async def test_a_lesson_outside_the_course_is_rejected(_db: None) -> None:
    course_id, _ = await _course(lessons=1)
    student = uuid.uuid4()
    enrollment_id = await _enrol(course_id, student)
    with pytest.raises(progress.LessonNotInCourseError):
        await _complete(enrollment_id, uuid.uuid4(), student)


async def test_only_the_student_records_their_progress(_db: None) -> None:
    course_id, lessons = await _course(lessons=1)
    enrollment_id = await _enrol(course_id, uuid.uuid4())
    with pytest.raises(enrollments.EnrollmentNotFoundError):
        await _complete(enrollment_id, lessons[0], uuid.uuid4())


async def test_withdrawn_and_completed_enrollments_are_terminal(_db: None) -> None:
    course_id, lessons = await _course(lessons=1)
    student = uuid.uuid4()
    finished = await _enrol(course_id, student)
    await _complete(finished, lessons[0], student)
    async with postgres.database.session_scope() as session:
        with pytest.raises(enrollments.EnrollmentCompletedError):
            await enrollments.withdraw(session, finished, current_user_id=student, role="student")

    other_course, other_lessons = await _course(lessons=1)
    left = await _enrol(other_course, student)
    async with postgres.database.session_scope() as session:
        await enrollments.withdraw(session, left, current_user_id=student, role="student")
    with pytest.raises(progress.EnrollmentNotActiveError):
        await _complete(left, other_lessons[0], student)


async def test_a_completed_enrollment_still_blocks_a_second_one(_db: None) -> None:
    course_id, lessons = await _course(lessons=1)
    student = uuid.uuid4()
    enrollment_id = await _enrol(course_id, student)
    await _complete(enrollment_id, lessons[0], student)
    with pytest.raises(enrollments.AlreadyEnrolledError):
        await _enrol(course_id, student)


async def test_rosters_use_the_name_from_user_registered(_db: None) -> None:
    course_id, _ = await _course(lessons=1)
    early, late = uuid.uuid4(), uuid.uuid4()
    await apply_user_event(user_event(early, full_name="Ada Lovelace"))
    await _enrol(course_id, early)
    # Enrolled before learning heard of them: the directory fills the name in at read.
    await _enrol(course_id, late)
    await apply_user_event(user_event(late, full_name="Alan Turing"))
    # Only students are kept, and never their email.
    assert (
        await apply_user_event(user_event(uuid.uuid4(), full_name="X", role="instructor")) is False
    )

    async with postgres.database.session_scope() as session:
        roster = await enrollments.course_roster(
            session,
            course_id,
            current_user_id=uuid.uuid4(),
            role="admin",
            status=None,
            limit=10,
            cursor=None,
        )
    assert sorted(item.student_name or "" for item in roster.items) == [
        "Ada Lovelace",
        "Alan Turing",
    ]


async def test_an_event_without_lesson_ids_keeps_the_known_lessons(_db: None) -> None:
    """A producer that predates lesson_ids must not wipe everyone's progress basis."""
    course_id, lessons = await _course(lessons=2)
    await apply_course_event(course_event(course_id, revision=2, title="Renamed"))
    student = uuid.uuid4()
    enrollment_id = await _enrol(course_id, student)
    view = await _complete(enrollment_id, lessons[0], student)
    assert view.lessons_total == 2
