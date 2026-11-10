import time
import uuid
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.security import HTTPAuthorizationCredentials

from platform_core.config import JWTVerifySettings
from platform_core.exceptions import ForbiddenError, UnauthorizedError
from platform_core.security import (
    CurrentUser,
    InvalidTokenError,
    authenticate,
    decode_access_token,
    roles_dependency,
)


@pytest.fixture
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def settings(signing_key: rsa.RSAPrivateKey, tmp_path: Path) -> JWTVerifySettings:
    public_path = tmp_path / "public.pem"
    public_path.write_bytes(
        signing_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return JWTVerifySettings(jwt_public_key_path=str(public_path))


def _token(key: rsa.RSAPrivateKey, **overrides: object) -> str:
    now = int(time.time())
    claims: dict[str, object] = {
        "sub": str(uuid.uuid4()),
        "role": "student",
        "type": "access",
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + 900,
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256")


def _bearer(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_decode_accepts_a_token_signed_by_the_matching_key(
    signing_key: rsa.RSAPrivateKey, settings: JWTVerifySettings
) -> None:
    payload = decode_access_token(_token(signing_key, role="admin"), settings=settings)
    assert payload["role"] == "admin"


def test_decode_rejects_a_token_that_is_not_an_access_token(
    signing_key: rsa.RSAPrivateKey, settings: JWTVerifySettings
) -> None:
    with pytest.raises(InvalidTokenError):
        decode_access_token(_token(signing_key, type="refresh"), settings=settings)


def test_decode_rejects_a_token_signed_by_another_key(settings: JWTVerifySettings) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(InvalidTokenError):
        decode_access_token(_token(other), settings=settings)


def test_authenticate_builds_the_current_user(
    signing_key: rsa.RSAPrivateKey, settings: JWTVerifySettings
) -> None:
    user_id = uuid.uuid4()
    current_user = authenticate(
        _bearer(_token(signing_key, sub=str(user_id), role="instructor")), settings=settings
    )
    assert current_user.id == user_id
    assert current_user.role == "instructor"


def test_authenticate_without_credentials_is_unauthorized(settings: JWTVerifySettings) -> None:
    with pytest.raises(UnauthorizedError):
        authenticate(None, settings=settings)


def test_authenticate_with_an_expired_token_is_unauthorized(
    signing_key: rsa.RSAPrivateKey, settings: JWTVerifySettings
) -> None:
    expired = _token(signing_key, exp=int(time.time()) - 60)
    with pytest.raises(UnauthorizedError):
        authenticate(_bearer(expired), settings=settings)


async def _current_student() -> CurrentUser:
    return CurrentUser(id=uuid.uuid4(), role="student", jti="x", expires_at=0)


async def test_roles_dependency_allows_a_matching_role() -> None:
    checker = roles_dependency(_current_student, "student", "admin")
    user = CurrentUser(id=uuid.uuid4(), role="student", jti="x", expires_at=0)
    assert await checker(current_user=user) is user


async def test_roles_dependency_rejects_a_non_matching_role() -> None:
    checker = roles_dependency(_current_student, "admin")
    user = CurrentUser(id=uuid.uuid4(), role="student", jti="x", expires_at=0)
    with pytest.raises(ForbiddenError):
        await checker(current_user=user)
