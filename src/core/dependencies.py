"""current_user, require_roles. Pagination params aren't here yet — nothing calls
cursor pagination yet (rule: infra moves to core/ on the second real use, not the
first); add it here the first time a list endpoint needs it.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.config import get_settings
from src.core.exceptions import ForbiddenError, UnauthorizedError
from src.core.security import InvalidTokenError, access_token_denylist_key, decode_access_token
from src.db.redis import get_redis

_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class CurrentUser:
    id: uuid.UUID
    role: str
    jti: str
    expires_at: int


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> CurrentUser:
    if credentials is None:
        raise UnauthorizedError("Missing bearer token")

    settings = get_settings().security
    try:
        payload = decode_access_token(credentials.credentials, settings=settings)
    except InvalidTokenError as exc:
        raise UnauthorizedError("Invalid or expired token") from exc

    jti: str = payload["jti"]
    if await get_redis().exists(access_token_denylist_key(jti)):
        raise UnauthorizedError("Token has been revoked")

    return CurrentUser(
        id=uuid.UUID(payload["sub"]), role=payload["role"], jti=jti, expires_at=payload["exp"]
    )


def require_roles(*roles: str) -> Callable[..., Any]:
    async def dependency(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if current_user.role not in roles:
            raise ForbiddenError(f"Requires one of roles: {', '.join(roles)}")
        return current_user

    return dependency
