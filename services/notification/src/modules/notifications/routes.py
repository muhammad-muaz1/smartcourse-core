"""The notification inbox. No send endpoint, ever: records are written only by event
consumers (API design §9) — an API that sends notifications is an abuse vector."""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.response import CursorPage, Envelope, success
from src.core.dependencies import CurrentUser, get_current_user
from src.db.postgres import get_session
from src.modules.notifications import service
from src.modules.notifications.schemas import NotificationPublic, ReadAllResult

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/me", response_model=Envelope[CursorPage[NotificationPublic]])
async def my_notifications_route(
    unread: bool = Query(default=False),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Envelope[CursorPage[NotificationPublic]]:
    page = await service.list_for_user(
        session, user_id=current_user.id, unread_only=unread, limit=limit, cursor=cursor
    )
    return success(page)


@router.post("/read-all", response_model=Envelope[ReadAllResult])
async def read_all_route(
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Envelope[ReadAllResult]:
    count = await service.mark_all_read(session, user_id=current_user.id)
    return success(ReadAllResult(marked_read=count))


@router.post("/{notification_id}/read", response_model=Envelope[NotificationPublic])
async def read_route(
    notification_id: uuid.UUID,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Envelope[NotificationPublic]:
    return success(await service.mark_read(session, notification_id, user_id=current_user.id))
