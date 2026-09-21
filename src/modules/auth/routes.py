"""HTTP layer: validate, call service, return. No business logic here — see service.py."""

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import Settings, get_settings
from src.core.dependencies import CurrentUser, get_current_user
from src.core.exceptions import NotFoundError
from src.core.response import Envelope, success
from src.db.postgres import get_session
from src.db.redis import get_redis
from src.modules.auth import service
from src.modules.auth.schemas import (
    ForgotPasswordRequest,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenPair,
    UserPublic,
)
from src.modules.users import service as users_service

router = APIRouter(prefix="/auth", tags=["auth"])

# The only NotificationSender implementation until Celery exists (API_DESIGN.md §3) —
# stateless, so one shared instance is fine.
_sender = service.LoggingNotificationSender()


@router.post("/register", status_code=status.HTTP_201_CREATED, response_model=Envelope[UserPublic])
async def register_route(
    body: RegisterRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Envelope[UserPublic]:
    client_ip = request.client.host if request.client else "unknown"
    user = await service.register(
        session,
        redis,
        email=body.email,
        password=body.password,
        full_name=body.full_name,
        role=body.role,
        client_ip=client_ip,
        settings=settings.security,
        sender=_sender,
    )
    return success(UserPublic.model_validate(user, from_attributes=True))


@router.post("/login", response_model=Envelope[TokenPair])
async def login_route(
    body: LoginRequest,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Envelope[TokenPair]:
    _, tokens = await service.login(
        session, redis, email=body.email, password=body.password, settings=settings.security
    )
    return success(tokens)


@router.post("/refresh", response_model=Envelope[TokenPair])
async def refresh_route(
    body: RefreshRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Envelope[TokenPair]:
    _, tokens = await service.refresh_tokens(
        session, raw_refresh_token=body.refresh_token, settings=settings.security
    )
    return success(tokens)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout_route(
    body: LogoutRequest,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> Response:
    await service.logout(
        session,
        redis,
        raw_refresh_token=body.refresh_token,
        access_token_jti=current_user.jti,
        access_token_expires_at=current_user.expires_at,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=Envelope[UserPublic])
async def me_route(
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Envelope[UserPublic]:
    user = await users_service.get_by_id(session, current_user.id)
    if user is None:
        raise NotFoundError("User not found")
    return success(UserPublic.model_validate(user, from_attributes=True))


@router.post("/password/forgot", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password_route(
    body: ForgotPasswordRequest,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    await service.forgot_password(
        session, redis, email=body.email, settings=settings.security, sender=_sender
    )
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/password/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password_route(
    body: ResetPasswordRequest,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> Response:
    await service.reset_password(session, redis, token=body.token, new_password=body.new_password)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
