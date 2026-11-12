"""Offline access-token verification and the request's authenticated principal.

Every service verifies tokens itself against identity's public key — no introspection
call, no shared secret (CLAUDE.md §9). Issuing tokens is identity's job alone and lives
in that service, never here.

Authentication is a dependency, not middleware, so OpenAPI documents which routes need
it. Each service builds its own ``get_current_user`` from ``authenticate`` because a
service may add its own checks on top (identity also consults its access-token
denylist).
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from platform_core.config import JWTVerifySettings
from platform_core.exceptions import ForbiddenError, UnauthorizedError

bearer_scheme = HTTPBearer(auto_error=False)


class InvalidTokenError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class CurrentUser:
    id: uuid.UUID
    role: str
    jti: str
    expires_at: int


@lru_cache
def load_public_key(path: str) -> RSAPublicKey:
    key = serialization.load_pem_public_key(Path(path).read_bytes())
    if not isinstance(key, RSAPublicKey):
        raise TypeError(f"{path} is not an RSA public key")
    return key


def decode_access_token(token: str, *, settings: JWTVerifySettings) -> dict[str, Any]:
    public_key = load_public_key(settings.jwt_public_key_path)
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            public_key,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "iat", "sub", "jti", "type"]},
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc

    if payload.get("type") != "access":
        raise InvalidTokenError("not an access token")
    return payload


def authenticate(
    credentials: HTTPAuthorizationCredentials | None, *, settings: JWTVerifySettings
) -> CurrentUser:
    if credentials is None:
        raise UnauthorizedError("Missing bearer token")
    try:
        payload = decode_access_token(credentials.credentials, settings=settings)
    except InvalidTokenError as exc:
        raise UnauthorizedError("Invalid or expired token") from exc
    return CurrentUser(
        id=uuid.UUID(payload["sub"]),
        role=payload["role"],
        jti=payload["jti"],
        expires_at=payload["exp"],
    )


def subject_from_authorization(
    authorization: str | None, *, settings: JWTVerifySettings
) -> str | None:
    """The verified ``sub`` of a ``Bearer`` header, or None if there is no valid access
    token. For middleware that must key state by user before any route runs."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    try:
        payload = decode_access_token(authorization[len("bearer ") :].strip(), settings=settings)
    except InvalidTokenError:
        return None
    return str(payload["sub"])


def roles_dependency(
    get_current_user: Callable[..., Awaitable[CurrentUser]], *roles: str
) -> Callable[..., Awaitable[CurrentUser]]:
    async def dependency(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if current_user.role not in roles:
            raise ForbiddenError(f"Requires one of roles: {', '.join(roles)}")
        return current_user

    return dependency
