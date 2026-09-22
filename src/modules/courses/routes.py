"""HTTP layer: validate, call service, return. No business logic here — see service.py."""

import uuid

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.dependencies import CurrentUser, get_current_user, require_roles
from src.core.response import CursorPage, Envelope, success
from src.db.postgres import get_session
from src.modules.courses import service
from src.modules.courses.models import CourseLevel, CourseStatus
from src.modules.courses.schemas import CourseCreate, CoursePublic, CourseUpdate

router = APIRouter(prefix="/courses", tags=["courses"])

# Module-level singleton, not `Depends(require_roles("instructor"))` inline: ruff's B008
# (correctly) still flags a nested call inside an otherwise-immutable Depends() default.
_require_instructor = require_roles("instructor")


@router.post("", status_code=status.HTTP_201_CREATED, response_model=Envelope[CoursePublic])
async def create_course_route(
    body: CourseCreate,
    current_user: CurrentUser = Depends(_require_instructor),
    session: AsyncSession = Depends(get_session),
) -> Envelope[CoursePublic]:
    course = await service.create_course(
        session,
        title=body.title,
        description=body.description,
        level=body.level,
        instructor_id=current_user.id,
    )
    return success(CoursePublic.model_validate(course, from_attributes=True))


@router.get("", response_model=Envelope[CursorPage[CoursePublic]])
async def list_courses_route(
    session: AsyncSession = Depends(get_session),
    q: str | None = Query(default=None, max_length=255),
    level: CourseLevel | None = None,
    course_status: CourseStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
) -> Envelope[CursorPage[CoursePublic]]:
    page = await service.list_courses(
        session, q=q, level=level, status=course_status, limit=limit, cursor=cursor
    )
    return success(page)


@router.get("/{course_id}", response_model=Envelope[CoursePublic])
async def get_course_route(
    course_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> Envelope[CoursePublic]:
    course = await service.get_course_or_404(session, course_id)
    return success(CoursePublic.model_validate(course, from_attributes=True))


@router.patch("/{course_id}", response_model=Envelope[CoursePublic])
async def update_course_route(
    course_id: uuid.UUID,
    body: CourseUpdate,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Envelope[CoursePublic]:
    course = await service.update_course(
        session,
        course_id,
        current_user_id=current_user.id,
        role=current_user.role,
        title=body.title,
        description=body.description,
        level=body.level,
    )
    return success(CoursePublic.model_validate(course, from_attributes=True))


@router.delete("/{course_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_course_route(
    course_id: uuid.UUID,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await service.archive_course(
        session, course_id, current_user_id=current_user.id, role=current_user.role
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
