"""``POST /courses/{id}/publish`` is ★ — Idempotency-Key required, enforced by the
middleware configured in app.py, not here."""

import uuid

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.response import Envelope, success
from src.config import get_settings
from src.core.dependencies import CurrentUser, get_current_user
from src.db.postgres import get_session
from src.modules.courses.schemas import PublishRunPublic
from src.modules.courses.service import publishing as service

router = APIRouter(prefix="/courses/{course_id}", tags=["publishing"])


@router.post(
    "/publish",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=Envelope[PublishRunPublic],
)
async def publish_course_route(
    course_id: uuid.UUID,
    response: Response,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Envelope[PublishRunPublic]:
    run = await service.request_publish(
        session, course_id, current_user_id=current_user.id, role=current_user.role
    )
    api_prefix = get_settings().app.api_prefix
    response.headers["Location"] = f"{api_prefix}/courses/{course_id}/publish-runs/{run.id}"
    return success(PublishRunPublic.model_validate(run, from_attributes=True))


@router.get("/publish-runs", response_model=Envelope[list[PublishRunPublic]])
async def list_publish_runs_route(
    course_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Envelope[list[PublishRunPublic]]:
    runs = await service.list_runs(
        session, course_id, current_user_id=current_user.id, role=current_user.role, limit=limit
    )
    return success([PublishRunPublic.model_validate(run, from_attributes=True) for run in runs])


@router.get("/publish-runs/{run_id}", response_model=Envelope[PublishRunPublic])
async def get_publish_run_route(
    course_id: uuid.UUID,
    run_id: uuid.UUID,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Envelope[PublishRunPublic]:
    run = await service.get_run(
        session, course_id, run_id, current_user_id=current_user.id, role=current_user.role
    )
    return success(PublishRunPublic.model_validate(run, from_attributes=True))
