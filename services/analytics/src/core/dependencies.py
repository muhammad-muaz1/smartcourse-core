"""current_user, require_roles for the analytics service.

Tokens are verified offline against identity's public key; there is no call to
identity on the request path. identity's access-token denylist is identity's data, so
a logged-out token keeps working here until it expires (≤15 minutes) — see
docs/adr/0010.
"""

from collections.abc import Awaitable, Callable

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials

from platform_core.security import CurrentUser, authenticate, bearer_scheme, roles_dependency
from src.config import get_settings

__all__ = ["CurrentUser", "get_current_user", "require_roles"]


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> CurrentUser:
    return authenticate(credentials, settings=get_settings().security)


def require_roles(*roles: str) -> Callable[..., Awaitable[CurrentUser]]:
    return roles_dependency(get_current_user, *roles)
