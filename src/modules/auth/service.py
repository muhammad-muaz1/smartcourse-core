"""Registration, login, refresh rotation with reuse detection, logout, password reset.

Every function takes the session (and Redis client, where needed) as a parameter —
never fetches its own — so the same function stays callable from a route, a task, or a
consumer unchanged, once those exist (API_DESIGN.md §12's first rule).
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Protocol

import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import SecuritySettings
from src.core.exceptions import ConflictError, RateLimitedError, UnauthorizedError
from src.core.security import (
    access_token_denylist_key,
    create_access_token,
    hash_password,
    verify_password,
)
from src.modules.auth.models import RefreshToken
from src.modules.auth.schemas import TokenPair
from src.modules.users import service as users_service
from src.modules.users.models import User, UserRole

logger = structlog.get_logger(__name__)


class NotificationSender(Protocol):
    async def send(self, *, to: str, subject: str, body: str) -> None: ...


class LoggingNotificationSender:
    """Only implementation until Celery exists — logs instead of sending. Swapping in a
    real sender later is a one-line change at the call site, not a route/service
    change (API_DESIGN.md §3)."""

    async def send(self, *, to: str, subject: str, body: str) -> None:
        await logger.ainfo("notification_stub_sent", to=to, subject=subject)


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _new_raw_token() -> str:
    return secrets.token_urlsafe(48)


def _token_pair(user: User, refresh_raw: str, *, settings: SecuritySettings) -> TokenPair:
    access_token = create_access_token(user_id=user.id, role=user.role, settings=settings)
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_raw,
        expires_in=settings.access_token_expires_minutes * 60,
    )


async def _issue_refresh_token(
    session: AsyncSession, *, user_id: uuid.UUID, family_id: uuid.UUID, settings: SecuritySettings
) -> str:
    raw = _new_raw_token()
    token = RefreshToken(
        user_id=user_id,
        family_id=family_id,
        token_hash=_hash_token(raw),
        expires_at=datetime.now(tz=UTC) + timedelta(days=settings.refresh_token_expires_days),
    )
    session.add(token)
    await session.flush()
    return raw


async def _revoke_active_tokens(
    session: AsyncSession, *, family_id: uuid.UUID | None = None, user_id: uuid.UUID | None = None
) -> None:
    assert (family_id is None) != (user_id is None), "revoke by exactly one of family_id/user_id"
    now = datetime.now(tz=UTC)
    if family_id is not None:
        condition = RefreshToken.family_id == family_id
    else:
        condition = RefreshToken.user_id == user_id
    result = await session.execute(
        select(RefreshToken).where(condition, RefreshToken.revoked_at.is_(None))
    )
    for token in result.scalars():
        token.revoked_at = now


async def register(
    session: AsyncSession,
    redis: Redis,
    *,
    email: str,
    password: str,
    full_name: str,
    role: UserRole,
    client_ip: str,
    settings: SecuritySettings,
    sender: NotificationSender,
) -> User:
    throttle_key = f"auth:register_throttle:{client_ip}"
    attempts = await redis.incr(throttle_key)
    if attempts == 1:
        await redis.expire(throttle_key, 3600)
    if attempts > settings.register_throttle_per_hour:
        raise RateLimitedError("Too many registration attempts from this address.")

    if await users_service.get_by_email(session, email) is not None:
        raise ConflictError("An account with this email already exists.")

    user = await users_service.create_user(
        session,
        email=email,
        hashed_password=hash_password(password),
        full_name=full_name,
        role=role,
    )
    await sender.send(to=email, subject="Welcome to SmartCourse", body=f"Welcome, {full_name}!")
    return user


async def login(
    session: AsyncSession, redis: Redis, *, email: str, password: str, settings: SecuritySettings
) -> tuple[User, TokenPair]:
    lockout_key = f"auth:failed_login:{email}"
    failures = await redis.get(lockout_key)
    if failures is not None and int(failures) >= settings.failed_login_max_attempts:
        raise RateLimitedError("Too many failed login attempts. Try again later.")

    user = await users_service.get_by_email(session, email)
    password_ok = user is not None and verify_password(
        password=password, password_hash=user.hashed_password
    )
    if user is None or not user.is_active or not password_ok:
        pipe = redis.pipeline()
        pipe.incr(lockout_key)
        pipe.expire(lockout_key, settings.failed_login_lockout_seconds)
        await pipe.execute()
        raise UnauthorizedError("Invalid email or password.")

    await redis.delete(lockout_key)

    family_id = uuid.uuid4()
    refresh_raw = await _issue_refresh_token(
        session, user_id=user.id, family_id=family_id, settings=settings
    )
    return user, _token_pair(user, refresh_raw, settings=settings)


async def refresh_tokens(
    session: AsyncSession, *, raw_refresh_token: str, settings: SecuritySettings
) -> tuple[User, TokenPair]:
    result = await session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == _hash_token(raw_refresh_token))
    )
    stored = result.scalar_one_or_none()
    if stored is None:
        raise UnauthorizedError("Invalid refresh token.")

    now = datetime.now(tz=UTC)

    if stored.revoked_at is not None:
        # This token was already rotated away (or its family already revoked) — being
        # handed it again means someone is replaying a stolen token. Burn the family.
        #
        # Deliberate exception to "services don't commit" (CLAUDE.md §8): raising
        # UnauthorizedError below makes get_session's dependency roll back the
        # transaction — which would silently undo this exact revocation, the one
        # thing this whole code path exists to do. Caught by actually exercising this
        # path end-to-end (register -> login -> refresh -> replay -> replay the
        # rotated-in token too) rather than by reading the code: the family-revoked
        # token kept working. Committing here, before raising, is what makes the
        # revocation survive the error response instead of being erased by it.
        await _revoke_active_tokens(session, family_id=stored.family_id)
        await session.commit()
        await logger.awarning(
            "refresh_token_reuse_detected",
            family_id=str(stored.family_id),
            user_id=str(stored.user_id),
        )
        raise UnauthorizedError("Refresh token has been revoked.")

    if stored.expires_at < now:
        raise UnauthorizedError("Refresh token has expired.")

    user = await users_service.get_by_id(session, stored.user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("Account is not available.")

    new_raw = await _issue_refresh_token(
        session, user_id=user.id, family_id=stored.family_id, settings=settings
    )
    stored.revoked_at = now

    return user, _token_pair(user, new_raw, settings=settings)


async def logout(
    session: AsyncSession,
    redis: Redis,
    *,
    raw_refresh_token: str,
    access_token_jti: str,
    access_token_expires_at: int,
) -> None:
    result = await session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == _hash_token(raw_refresh_token))
    )
    stored = result.scalar_one_or_none()
    if stored is not None:
        await _revoke_active_tokens(session, family_id=stored.family_id)

    ttl_seconds = max(access_token_expires_at - int(datetime.now(tz=UTC).timestamp()), 1)
    await redis.set(access_token_denylist_key(access_token_jti), "1", ex=ttl_seconds)


async def forgot_password(
    session: AsyncSession,
    redis: Redis,
    *,
    email: str,
    settings: SecuritySettings,
    sender: NotificationSender,
) -> None:
    user = await users_service.get_by_email(session, email)
    if user is None:
        # Same response either way — do not leak which emails are registered.
        return

    raw_token = _new_raw_token()
    key = f"auth:password_reset:{_hash_token(raw_token)}"
    await redis.set(key, str(user.id), ex=settings.password_reset_token_expires_minutes * 60)
    await sender.send(
        to=email, subject="Reset your SmartCourse password", body=f"Reset token: {raw_token}"
    )


async def reset_password(
    session: AsyncSession, redis: Redis, *, token: str, new_password: str
) -> None:
    key = f"auth:password_reset:{_hash_token(token)}"
    user_id_raw = await redis.getdel(key)
    if user_id_raw is None:
        raise UnauthorizedError("Invalid or expired reset token.")
    # The client is always constructed with decode_responses=True (db/redis.py), so
    # this is str at runtime — redis-py's stubs just don't express that.
    assert isinstance(user_id_raw, str)

    user = await users_service.get_by_id(session, uuid.UUID(user_id_raw))
    if user is None:
        raise UnauthorizedError("Invalid or expired reset token.")

    user.hashed_password = hash_password(new_password)
    await _revoke_active_tokens(session, user_id=user.id)
