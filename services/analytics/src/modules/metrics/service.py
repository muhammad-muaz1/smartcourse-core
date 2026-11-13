"""Metric queries over the fact tables, cached briefly in Redis, plus who may see what.

Numbers are eventually consistent — typically seconds behind the services that own the
facts. Every result carries ``as_of`` (when analytics last applied an event), so the lag
is visible rather than implied (API design §10). A cache miss, or Redis being down, only
costs a query: the cache is never the source of truth.

Every function takes the session first and never commits (CLAUDE.md §6).
"""

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import Float, case, cast, distinct, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.exceptions import ForbiddenError, NotFoundError, ValidationError
from src.db.inbox import ProcessedEvent
from src.modules.metrics.models import CourseFact, EnrollmentFact, FailureRecord, UserFact
from src.modules.metrics.schemas import (
    CompletionStats,
    CourseDashboard,
    CourseLoad,
    EnrollmentBucket,
    EnrollmentSeries,
    FailureGroup,
    FailureItem,
    FailureSummary,
    Granularity,
    Overview,
    Period,
    PopularCourse,
    PopularCourses,
    TimeToComplete,
)

logger = structlog.get_logger(__name__)

ADMIN_ROLE = "admin"  # JWT claim values; identity's enum is never imported (CLAUDE.md §4)
_MAX_BUCKETS = 400
_PERIOD_DAYS: dict[str, int | None] = {"7d": 7, "30d": 30, "90d": 90, "all": None}
_STEP: dict[str, str] = {"day": "1 day", "week": "1 week", "month": "1 month"}


class CourseNotFoundError(NotFoundError):
    code = "COURSE_NOT_FOUND"
    message = "Course not found"


# --- caching -------------------------------------------------------------------------


async def as_of(session: AsyncSession) -> datetime | None:
    """When analytics last applied an event — the moment every projection reflects."""
    return await session.scalar(select(func.max(ProcessedEvent.processed_at)))


async def cached[M: BaseModel](
    redis: Redis,
    *,
    name: str,
    params: dict[str, Any],
    ttl_seconds: int,
    model: type[M],
    compute: Callable[[], Awaitable[tuple[M, datetime | None]]],
) -> tuple[M, datetime | None]:
    digest = hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()
    key = f"analytics:{name}:{digest[:32]}"
    try:
        hit = await redis.get(key)
    except Exception as exc:  # noqa: BLE001 - Redis down: slower, never wrong
        await logger.awarning("metrics_cache_unavailable", detail=str(exc))
        hit = None
    if hit is not None:
        stored = json.loads(hit)
        stamp = stored["as_of"]
        return model.model_validate(stored["data"]), (
            datetime.fromisoformat(stamp) if stamp else None
        )

    result, stamp_dt = await compute()
    payload = {
        "data": result.model_dump(mode="json"),
        "as_of": stamp_dt.isoformat() if stamp_dt else None,
    }
    try:
        await redis.set(key, json.dumps(payload), ex=ttl_seconds)
    except Exception as exc:  # noqa: BLE001 - same: the answer is still correct
        await logger.awarning("metrics_cache_unavailable", detail=str(exc))
    return result, stamp_dt


# --- access --------------------------------------------------------------------------


async def ensure_course_access(
    session: AsyncSession, course_id: uuid.UUID, *, current_user_id: uuid.UUID, role: str
) -> CourseFact:
    """Course-level metrics: the course's instructor and admins. Ownership comes from
    the projection itself — no call to course."""
    course = await session.get(CourseFact, course_id)
    if course is None:
        raise CourseNotFoundError()
    if role != ADMIN_ROLE and course.instructor_id != current_user_id:
        raise ForbiddenError("Only the course's instructor or an admin can view its metrics")
    return course


async def scope_course(
    session: AsyncSession, course_id: uuid.UUID | None, *, current_user_id: uuid.UUID, role: str
) -> None:
    """Platform-wide numbers are admin-only; an instructor must name one of their own
    courses."""
    if course_id is not None:
        await ensure_course_access(session, course_id, current_user_id=current_user_id, role=role)
    elif role != ADMIN_ROLE:
        raise ForbiddenError("Platform-wide figures are for admins; pass one of your course_id")


# --- metrics -------------------------------------------------------------------------


async def overview(session: AsyncSession) -> Overview:
    # .all(), not the Result itself: Result has .keys(), so dict() would treat it as a
    # mapping and index it.
    rows = await session.execute(select(UserFact.role, func.count()).group_by(UserFact.role))
    roles: dict[str, int] = dict(rows.tuples().all())
    published = await session.scalar(select(func.count()).where(CourseFact.status == "published"))
    enrollments = await session.scalar(select(func.count()).select_from(EnrollmentFact))
    return Overview(
        total_students=roles.get("student", 0),
        total_instructors=roles.get("instructor", 0),
        total_courses_published=published or 0,
        total_enrollments=enrollments or 0,
    )


async def enrollment_series(
    session: AsyncSession,
    *,
    start: datetime,
    end: datetime,
    granularity: Granularity,
    course_id: uuid.UUID | None,
) -> EnrollmentSeries:
    if end <= start:
        raise ValidationError("'to' must be after 'from'")
    approx_days = {"day": 1, "week": 7, "month": 28}[granularity]
    if (end - start).days / approx_days > _MAX_BUCKETS:
        raise ValidationError(f"At most {_MAX_BUCKETS} buckets; widen the granularity")
    # generate_series fills empty buckets with zero, so a chart has no gaps; the
    # three-argument date_trunc pins bucket edges to UTC whatever the session's zone.
    rows = await session.execute(
        text(
            """
            SELECT b.bucket, count(e.enrollment_id)
            FROM generate_series(
                date_trunc(:g, CAST(:start AS timestamptz), 'UTC'),
                date_trunc(:g, CAST(:end AS timestamptz), 'UTC'),
                -- bound as text: asyncpg wants a timedelta for an interval, and a
                -- calendar month isn't one
                CAST(CAST(:step AS text) AS interval)
            ) AS b(bucket)
            LEFT JOIN enrollment_facts e
              ON date_trunc(:g, e.enrolled_at, 'UTC') = b.bucket
             AND e.enrolled_at >= :start AND e.enrolled_at < :end
             AND (CAST(:course_id AS uuid) IS NULL OR e.course_id = CAST(:course_id AS uuid))
            GROUP BY b.bucket
            ORDER BY b.bucket
            """
        ),
        {
            "g": granularity,
            "start": start,
            "end": end,
            "step": _STEP[granularity],
            "course_id": course_id,
        },
    )
    return EnrollmentSeries(
        granularity=granularity,
        start=start,
        end=end,
        course_id=course_id,
        buckets=[EnrollmentBucket(period_start=b, enrollments=n) for b, n in rows.tuples()],
    )


def _course_filter(course_id: uuid.UUID | None) -> list[Any]:
    return [EnrollmentFact.course_id == course_id] if course_id is not None else []


async def completion(session: AsyncSession, *, course_id: uuid.UUID | None) -> CompletionStats:
    total, withdrawn, completed = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(EnrollmentFact.status == "withdrawn"),
                func.count().filter(EnrollmentFact.status == "completed"),
            ).where(*_course_filter(course_id))
        )
    ).one()
    cohort = total - withdrawn
    return CompletionStats(
        course_id=course_id,
        enrollments=total,
        withdrawn=withdrawn,
        completed=completed,
        completion_rate=round(completed / cohort, 4) if cohort else 0.0,
    )


async def time_to_complete(session: AsyncSession, *, course_id: uuid.UUID | None) -> TimeToComplete:
    seconds = func.extract("epoch", EnrollmentFact.completed_at - EnrollmentFact.enrolled_at)
    count, average = (
        await session.execute(
            select(func.count(), func.avg(seconds)).where(
                EnrollmentFact.status == "completed", *_course_filter(course_id)
            )
        )
    ).one()
    hours = round(float(average) / 3600, 2) if average is not None else None
    return TimeToComplete(
        course_id=course_id,
        completed=count,
        average_hours=hours,
        average_days=round(hours / 24, 2) if hours is not None else None,
    )


async def popular_courses(session: AsyncSession, *, period: Period, limit: int) -> PopularCourses:
    days = _PERIOD_DAYS[period]
    enrollments = func.count(EnrollmentFact.enrollment_id).label("enrollments")
    stmt = (
        select(CourseFact.course_id, CourseFact.title, enrollments)
        .join(EnrollmentFact, EnrollmentFact.course_id == CourseFact.course_id)
        # Only courses a student could enrol in today; an archived course isn't popular.
        .where(CourseFact.status == "published")
        .group_by(CourseFact.course_id, CourseFact.title)
        .order_by(enrollments.desc(), CourseFact.title)
        .limit(limit)
    )
    if days is not None:
        stmt = stmt.where(EnrollmentFact.enrolled_at >= datetime.now(tz=UTC) - timedelta(days=days))
    rows = (await session.execute(stmt)).tuples()
    return PopularCourses(
        period=period,
        items=[PopularCourse(course_id=c, title=t, enrollments=n) for c, t, n in rows],
    )


async def course_load(session: AsyncSession) -> CourseLoad:
    students, current = (
        await session.execute(
            select(func.count(distinct(EnrollmentFact.student_id)), func.count()).where(
                EnrollmentFact.status != "withdrawn"
            )
        )
    ).one()
    registered = await session.scalar(select(func.count()).where(UserFact.role == "student")) or 0
    return CourseLoad(
        students_with_enrollments=students,
        registered_students=registered,
        current_enrollments=current,
        average_per_enrolled_student=round(current / students, 2) if students else 0.0,
        average_per_registered_student=round(current / registered, 2) if registered else 0.0,
    )


async def course_dashboard(session: AsyncSession, course: CourseFact) -> CourseDashboard:
    status = EnrollmentFact.status
    seconds = func.extract("epoch", EnrollmentFact.completed_at - EnrollmentFact.enrolled_at)
    total, active, withdrawn, completed, average = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(status == "active"),
                func.count().filter(status == "withdrawn"),
                func.count().filter(status == "completed"),
                func.avg(case((status == "completed", cast(seconds, Float)))),
            ).where(EnrollmentFact.course_id == course.course_id)
        )
    ).one()
    cohort = total - withdrawn
    return CourseDashboard(
        course_id=course.course_id,
        title=course.title,
        status=course.status,
        enrollments=total,
        active=active,
        withdrawn=withdrawn,
        completed=completed,
        completion_rate=round(completed / cohort, 4) if cohort else 0.0,
        average_hours_to_complete=round(float(average) / 3600, 2) if average is not None else None,
    )


async def failures(
    session: AsyncSession,
    *,
    source: str | None,
    start: datetime | None,
    end: datetime | None,
    recent: int,
) -> FailureSummary:
    filters: list[Any] = []
    if source is not None:
        filters.append(FailureRecord.source == source)
    if start is not None:
        filters.append(FailureRecord.occurred_at >= start)
    if end is not None:
        filters.append(FailureRecord.occurred_at < end)
    count = func.count().label("count")
    groups = (
        await session.execute(
            select(
                FailureRecord.source,
                FailureRecord.service,
                FailureRecord.subject,
                count,
                func.max(FailureRecord.occurred_at),
            )
            .where(*filters)
            .group_by(FailureRecord.source, FailureRecord.service, FailureRecord.subject)
            .order_by(count.desc())
        )
    ).tuples()
    group_list = [
        FailureGroup(source=s, service=sv, subject=sj, count=n, last_seen=last)
        for s, sv, sj, n, last in groups
    ]
    latest = (
        await session.execute(
            select(FailureRecord)
            .where(*filters)
            .order_by(FailureRecord.occurred_at.desc())
            .limit(recent)
        )
    ).scalars()
    return FailureSummary(
        total=sum(group.count for group in group_list),
        groups=group_list,
        recent=[FailureItem.model_validate(row, from_attributes=True) for row in latest],
    )
