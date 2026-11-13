"""``/analytics/*`` — every endpoint is a Projection read (API design §10): served from
analytics_db, cached in Redis, and returned with ``meta.as_of``. No business logic here."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.response import Envelope, success
from src.config import Settings, get_settings
from src.core.dependencies import CurrentUser, require_roles
from src.db.postgres import get_session
from src.db.redis import get_redis
from src.modules.metrics import service
from src.modules.metrics.schemas import (
    CompletionStats,
    CourseDashboard,
    CourseLoad,
    EnrollmentSeries,
    FailureSummary,
    Granularity,
    Overview,
    Period,
    PopularCourses,
    TimeToComplete,
)

router = APIRouter(prefix="/analytics", tags=["analytics"])

_admin = require_roles("admin")
_staff = require_roles("admin", "instructor")
_anyone = require_roles("admin", "instructor", "student")


@router.get("/overview", response_model=Envelope[Overview])
async def overview_route(
    _user: CurrentUser = Depends(_staff),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Envelope[Overview]:
    async def compute() -> tuple[Overview, datetime | None]:
        return await service.overview(session), await service.as_of(session)

    data, as_of = await service.cached(
        redis,
        name="overview",
        params={},
        ttl_seconds=settings.cache.overview_seconds,
        model=Overview,
        compute=compute,
    )
    return success(data, as_of=as_of)


@router.get("/enrollments", response_model=Envelope[EnrollmentSeries])
async def enrollments_route(
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
    granularity: Granularity = Query(default="day"),
    course_id: uuid.UUID | None = None,
    user: CurrentUser = Depends(_staff),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Envelope[EnrollmentSeries]:
    await service.scope_course(session, course_id, current_user_id=user.id, role=user.role)
    end = end or datetime.now(tz=UTC)
    start = start or end - timedelta(days=30)

    async def compute() -> tuple[EnrollmentSeries, datetime | None]:
        series = await service.enrollment_series(
            session, start=start, end=end, granularity=granularity, course_id=course_id
        )
        return series, await service.as_of(session)

    data, as_of = await service.cached(
        redis,
        name="enrollments",
        params={"start": start, "end": end, "g": granularity, "course": course_id},
        ttl_seconds=settings.cache.timeseries_seconds,
        model=EnrollmentSeries,
        compute=compute,
    )
    return success(data, as_of=as_of)


@router.get("/completion", response_model=Envelope[CompletionStats])
async def completion_route(
    course_id: uuid.UUID | None = None,
    user: CurrentUser = Depends(_staff),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Envelope[CompletionStats]:
    await service.scope_course(session, course_id, current_user_id=user.id, role=user.role)

    async def compute() -> tuple[CompletionStats, datetime | None]:
        return await service.completion(session, course_id=course_id), await service.as_of(session)

    data, as_of = await service.cached(
        redis,
        name="completion",
        params={"course": course_id},
        ttl_seconds=settings.cache.timeseries_seconds,
        model=CompletionStats,
        compute=compute,
    )
    return success(data, as_of=as_of)


@router.get("/time-to-complete", response_model=Envelope[TimeToComplete])
async def time_to_complete_route(
    course_id: uuid.UUID | None = None,
    user: CurrentUser = Depends(_staff),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Envelope[TimeToComplete]:
    await service.scope_course(session, course_id, current_user_id=user.id, role=user.role)

    async def compute() -> tuple[TimeToComplete, datetime | None]:
        return (
            await service.time_to_complete(session, course_id=course_id),
            await service.as_of(session),
        )

    data, as_of = await service.cached(
        redis,
        name="time_to_complete",
        params={"course": course_id},
        ttl_seconds=settings.cache.timeseries_seconds,
        model=TimeToComplete,
        compute=compute,
    )
    return success(data, as_of=as_of)


@router.get("/courses/popular", response_model=Envelope[PopularCourses])
async def popular_route(
    period: Period = Query(default="30d"),
    limit: int = Query(default=10, ge=1, le=50),
    _user: CurrentUser = Depends(_anyone),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Envelope[PopularCourses]:
    async def compute() -> tuple[PopularCourses, datetime | None]:
        return (
            await service.popular_courses(session, period=period, limit=limit),
            await service.as_of(session),
        )

    data, as_of = await service.cached(
        redis,
        name="popular",
        params={"period": period, "limit": limit},
        ttl_seconds=settings.cache.timeseries_seconds,
        model=PopularCourses,
        compute=compute,
    )
    return success(data, as_of=as_of)


@router.get("/students/course-load", response_model=Envelope[CourseLoad])
async def course_load_route(
    _user: CurrentUser = Depends(_admin),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Envelope[CourseLoad]:
    async def compute() -> tuple[CourseLoad, datetime | None]:
        return await service.course_load(session), await service.as_of(session)

    data, as_of = await service.cached(
        redis,
        name="course_load",
        params={},
        ttl_seconds=settings.cache.course_load_seconds,
        model=CourseLoad,
        compute=compute,
    )
    return success(data, as_of=as_of)


@router.get("/courses/{course_id}", response_model=Envelope[CourseDashboard])
async def course_dashboard_route(
    course_id: uuid.UUID,
    user: CurrentUser = Depends(_staff),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Envelope[CourseDashboard]:
    course = await service.ensure_course_access(
        session, course_id, current_user_id=user.id, role=user.role
    )

    async def compute() -> tuple[CourseDashboard, datetime | None]:
        return await service.course_dashboard(session, course), await service.as_of(session)

    data, as_of = await service.cached(
        redis,
        name="course_dashboard",
        params={"course": course_id},
        ttl_seconds=settings.cache.timeseries_seconds,
        model=CourseDashboard,
        compute=compute,
    )
    return success(data, as_of=as_of)


@router.get("/failures", response_model=Envelope[FailureSummary])
async def failures_route(
    source: Literal["workflow", "dead_letter"] | None = Query(default=None, alias="type"),
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
    recent: int = Query(default=20, ge=0, le=100),
    _user: CurrentUser = Depends(_admin),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Envelope[FailureSummary]:
    async def compute() -> tuple[FailureSummary, datetime | None]:
        summary = await service.failures(
            session, source=source, start=start, end=end, recent=recent
        )
        return summary, await service.as_of(session)

    data, as_of = await service.cached(
        redis,
        name="failures",
        params={"source": source, "start": start, "end": end, "recent": recent},
        ttl_seconds=settings.cache.failures_seconds,
        model=FailureSummary,
        compute=compute,
    )
    return success(data, as_of=as_of)
