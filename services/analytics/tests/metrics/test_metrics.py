"""Projections and the brief's nine metrics, against a real Postgres (Docker or
SMARTCOURSE_TEST_POSTGRES_URL)."""

import uuid
from datetime import timedelta

from src.db import postgres
from src.modules.metrics import service
from src.modules.metrics.models import EnrollmentFact
from tests.metrics.helpers import (
    NOW,
    apply,
    course,
    dead_letter,
    enrollment,
    user,
    workflow_failure,
)


async def _fact(enrollment_id: uuid.UUID) -> EnrollmentFact:
    async with postgres.database.session_scope() as session:
        fact = await session.get(EnrollmentFact, enrollment_id)
        assert fact is not None
        return fact


class TestProjections:
    async def test_an_older_event_never_moves_a_fact_backwards(self, _db: None) -> None:
        """A backfill or replay interleaved with live traffic arrives out of order."""
        enrollment_id, course_id = uuid.uuid4(), uuid.uuid4()
        await apply(
            enrollment(
                enrollment_id, course_id, status="completed", completed_after=timedelta(hours=2)
            ),
            enrollment(enrollment_id, course_id, status="active", at=NOW - timedelta(hours=3)),
        )
        assert (await _fact(enrollment_id)).status == "completed"

    async def test_replaying_the_same_facts_counts_them_once(self, _db: None) -> None:
        instructor = uuid.uuid4()
        events = [user("student"), course(uuid.uuid4(), instructor_id=instructor)]
        await apply(*events, *events)
        async with postgres.database.session_scope() as session:
            overview = await service.overview(session)
        assert (overview.total_students, overview.total_courses_published) == (1, 1)


class TestMetrics:
    async def _cohort(self) -> uuid.UUID:
        """One course: 4 enrolments — 2 completed (2h and 4h), 1 withdrawn, 1 active."""
        course_id = uuid.uuid4()
        await apply(course(course_id, instructor_id=uuid.uuid4(), title="Popular"))
        for status, after in (
            ("completed", timedelta(hours=2)),
            ("completed", timedelta(hours=4)),
            ("withdrawn", None),
            ("active", None),
        ):
            await apply(enrollment(uuid.uuid4(), course_id, status=status, completed_after=after))
        return course_id

    async def test_overview_counts_people_and_published_courses(self, _db: None) -> None:
        await apply(
            user("student"),
            user("student"),
            user("instructor"),
            user("admin"),
            course(uuid.uuid4(), instructor_id=uuid.uuid4()),
            course(uuid.uuid4(), instructor_id=uuid.uuid4(), status="draft"),
        )
        async with postgres.database.session_scope() as session:
            overview = await service.overview(session)
            stamp = await service.as_of(session)
        assert (overview.total_students, overview.total_instructors) == (2, 1)
        assert overview.total_courses_published == 1
        assert stamp is None  # nothing went through the inbox in this test

    async def test_completion_rate_excludes_withdrawals(self, _db: None) -> None:
        course_id = await self._cohort()
        async with postgres.database.session_scope() as session:
            stats = await service.completion(session, course_id=course_id)
            timing = await service.time_to_complete(session, course_id=course_id)
        # 2 completed out of the 3 who stayed.
        assert (stats.enrollments, stats.withdrawn, stats.completed) == (4, 1, 2)
        assert stats.completion_rate == round(2 / 3, 4)
        assert (timing.completed, timing.average_hours) == (2, 3.0)

    async def test_popular_courses_and_course_load(self, _db: None) -> None:
        busy = await self._cohort()
        quiet = uuid.uuid4()
        student = uuid.uuid4()
        await apply(
            course(quiet, instructor_id=uuid.uuid4(), title="Quiet"),
            enrollment(uuid.uuid4(), quiet, student_id=student),
            user("student"),
        )
        async with postgres.database.session_scope() as session:
            popular = await service.popular_courses(session, period="7d", limit=5)
            load = await service.course_load(session)
        assert [item.course_id for item in popular.items] == [busy, quiet]
        # 4 current (non-withdrawn) enrolments across 4 distinct students.
        assert (load.current_enrollments, load.students_with_enrollments) == (4, 4)
        assert load.average_per_enrolled_student == 1.0

    async def test_enrollments_over_time_fill_empty_buckets(self, _db: None) -> None:
        course_id = uuid.uuid4()
        await apply(
            course(course_id, instructor_id=uuid.uuid4()),
            enrollment(uuid.uuid4(), course_id, enrolled_at=NOW - timedelta(days=2)),
            enrollment(uuid.uuid4(), course_id, enrolled_at=NOW),
            enrollment(uuid.uuid4(), course_id, enrolled_at=NOW),
        )
        async with postgres.database.session_scope() as session:
            series = await service.enrollment_series(
                session,
                start=NOW - timedelta(days=3),
                end=NOW + timedelta(minutes=1),
                granularity="day",
                course_id=course_id,
            )
        counts = [bucket.enrollments for bucket in series.buckets]
        assert len(counts) == 4 and sum(counts) == 3 and counts[-1] == 2 and 0 in counts

    async def test_failures_group_workflows_and_dead_letters(self, _db: None) -> None:
        await apply(workflow_failure("run-1"), workflow_failure("run-2"), dead_letter())
        async with postgres.database.session_scope() as session:
            summary = await service.failures(session, source=None, start=None, end=None, recent=10)
            only_dlq = await service.failures(
                session, source="dead_letter", start=None, end=None, recent=10
            )
        assert summary.total == 3
        assert {(g.source, g.subject, g.count) for g in summary.groups} == {
            ("workflow", "course.publish", 2),
            ("dead_letter", "smartcourse.enrollment.created.v1", 1),
        }
        assert only_dlq.total == 1
