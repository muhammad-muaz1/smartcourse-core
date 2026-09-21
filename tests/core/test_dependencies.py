"""Requires Docker (testcontainers spins up a real Redis) — get_current_user checks a
denylist on every call, so it can't be exercised without one.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from testcontainers.community.redis import RedisContainer

import src.core.dependencies as deps_module
from src.config import RedisSettings, SecuritySettings
from src.core.dependencies import CurrentUser, get_current_user, require_roles
from src.core.exceptions import ForbiddenError, UnauthorizedError
from src.core.security import access_token_denylist_key, create_access_token, decode_access_token
from src.db import redis as redis_db


@pytest.fixture
async def _redis(redis_container: RedisContainer) -> AsyncIterator[None]:
    url = f"redis://{redis_container.get_container_host_ip()}:{redis_container.get_exposed_port(6379)}/0"
    redis_db.init_redis(RedisSettings(url=url))
    try:
        yield
    finally:
        await redis_db.get_redis().flushdb()
        await redis_db.close_redis()


@pytest.fixture(autouse=True)
def _patch_settings(security_settings: SecuritySettings, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_current_user reads settings via src.core.dependencies.get_settings() — point
    it at the test's throwaway keypair instead of the real one in secrets/."""

    class _FakeSettings:
        security = security_settings

    monkeypatch.setattr(deps_module, "get_settings", lambda: _FakeSettings())


def _bearer(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


async def test_missing_credentials_is_unauthorized(_redis: None) -> None:
    with pytest.raises(UnauthorizedError):
        await get_current_user(credentials=None)


async def test_valid_token_resolves_to_current_user(
    _redis: None, security_settings: SecuritySettings
) -> None:
    user_id = uuid.uuid4()
    token = create_access_token(user_id=user_id, role="instructor", settings=security_settings)

    current_user = await get_current_user(credentials=_bearer(token))

    assert current_user.id == user_id
    assert current_user.role == "instructor"


async def test_denylisted_token_is_rejected(
    _redis: None, security_settings: SecuritySettings
) -> None:
    token = create_access_token(user_id=uuid.uuid4(), role="student", settings=security_settings)
    jti = decode_access_token(token, settings=security_settings)["jti"]
    await redis_db.get_redis().set(access_token_denylist_key(jti), "1", ex=60)

    with pytest.raises(UnauthorizedError):
        await get_current_user(credentials=_bearer(token))


async def test_garbage_token_is_rejected(_redis: None) -> None:
    with pytest.raises(UnauthorizedError):
        await get_current_user(credentials=_bearer("not-a-real-jwt"))


async def test_require_roles_allows_a_matching_role() -> None:
    checker = require_roles("admin", "instructor")
    current_user = CurrentUser(id=uuid.uuid4(), role="instructor", jti="x", expires_at=0)

    result = await checker(current_user=current_user)

    assert result is current_user


async def test_require_roles_rejects_a_non_matching_role() -> None:
    checker = require_roles("admin")
    current_user = CurrentUser(id=uuid.uuid4(), role="student", jti="x", expires_at=0)

    with pytest.raises(ForbiddenError):
        await checker(current_user=current_user)
