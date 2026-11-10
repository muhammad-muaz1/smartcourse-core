"""current_user, require_roles for identity.

On top of the offline signature check every service does, identity also consults its
access-token denylist, so a logged-out token stops working here immediately. Other
services only see revocation when the token expires (≤15 minutes) — see
docs/adr/0010.
"""

from collections.abc import Awaitable, Callable

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials

from platform_core.exceptions import UnauthorizedError
from platform_core.security import CurrentUser, authenticate, bearer_scheme, roles_dependency
from src.config import get_settings
from src.core.security import access_token_denylist_key
from src.db.redis import get_redis

__all__ = ["CurrentUser", "get_current_user", "require_roles"]


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> CurrentUser:
    current_user = authenticate(credentials, settings=get_settings().security)
    if await get_redis().exists(access_token_denylist_key(current_user.jti)):
        raise UnauthorizedError("Token has been revoked")
    return current_user


def require_roles(*roles: str) -> Callable[..., Awaitable[CurrentUser]]:
    return roles_dependency(get_current_user, *roles)
