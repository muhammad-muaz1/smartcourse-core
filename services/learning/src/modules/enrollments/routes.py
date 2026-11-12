"""HTTP layer: validate, call service, return. No business logic here — see service.py.

``POST /courses/{id}/enrollments`` is ★ — Idempotency-Key required, enforced by the
middleware configured in app.py. The gateway routes the enrollment sub-paths of
``/courses`` and ``/users`` here; everything else under those prefixes belongs to course
and identity.
"""

import uuid

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.response import CursorPage, Envelope, success
from src.core.dependencies import CurrentUser, get_current_user, require_roles
from src.db.postgres import get_session
from src.modules.enrollments import service
from src.modules.enrollments.models import EnrollmentStatus
from src.modules.enrollments.schemas import EnrollmentPublic

router = APIRouter(tags=["enrollments"])

_require_student = require_roles("student")


@router.post(
    "/courses/{course_id}/enrollments",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[EnrollmentPublic],
)
async def enroll_route(
    course_id: uuid.UUID,
    current_user: CurrentUser = Depends(_require_student),
    session: AsyncSession = Depends(get_session),
) -> Envelope[EnrollmentPublic]:
    enrollment = await service.enroll(session, course_id=course_id, student_id=current_user.id)
    return success(
        await service.get_enrollment(
            session, enrollment.id, current_user_id=current_user.id, role=current_user.role
        )
    )


@router.get(
    "/courses/{course_id}/enrollments", response_model=Envelope[CursorPage[EnrollmentPublic]]
)
async def course_roster_route(
    course_id: uuid.UUID,
    enrollment_status: EnrollmentStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Envelope[CursorPage[EnrollmentPublic]]:
    page = await service.course_roster(
        session,
        course_id,
        current_user_id=current_user.id,
        role=current_user.role,
        status=enrollment_status,
        limit=limit,
        cursor=cursor,
    )
    return success(page)


# Declared before /enrollments/{enrollment_id} so "me" is never parsed as an id.
@router.get("/enrollments/me", response_model=Envelope[CursorPage[EnrollmentPublic]])
async def my_enrollments_route(
    enrollment_status: EnrollmentStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Envelope[CursorPage[EnrollmentPublic]]:
    page = await service.list_for_student(
        session, student_id=current_user.id, status=enrollment_status, limit=limit, cursor=cursor
    )
    return success(page)


@router.get("/enrollments/{enrollment_id}", response_model=Envelope[EnrollmentPublic])
async def get_enrollment_route(
    enrollment_id: uuid.UUID,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Envelope[EnrollmentPublic]:
    enrollment = await service.get_enrollment(
        session, enrollment_id, current_user_id=current_user.id, role=current_user.role
    )
    return success(enrollment)


@router.delete("/enrollments/{enrollment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def withdraw_route(
    enrollment_id: uuid.UUID,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await service.withdraw(
        session, enrollment_id, current_user_id=current_user.id, role=current_user.role
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/users/{user_id}/enrollment-history",
    response_model=Envelope[CursorPage[EnrollmentPublic]],
)
async def enrollment_history_route(
    user_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Envelope[CursorPage[EnrollmentPublic]]:
    page = await service.enrollment_history(
        session,
        user_id,
        current_user_id=current_user.id,
        role=current_user.role,
        limit=limit,
        cursor=cursor,
    )
    return success(page)
