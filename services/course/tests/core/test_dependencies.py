"""The course service verifies identity-issued tokens offline, with only the public key.
Tokens are signed here with PyJWT directly: importing identity's token code would be
exactly the cross-service coupling this service must not have.
"""

import time
import uuid
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.security import HTTPAuthorizationCredentials

import src.core.dependencies as deps_module
from platform_core.config import JWTVerifySettings
from platform_core.exceptions import ForbiddenError, UnauthorizedError
from src.core.dependencies import CurrentUser, get_current_user, require_roles


@pytest.fixture
def private_pem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> bytes:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_path = tmp_path / "public.pem"
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    class _FakeSettings:
        security = JWTVerifySettings(jwt_public_key_path=str(public_path))

    monkeypatch.setattr(deps_module, "get_settings", lambda: _FakeSettings())
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _token(private_pem: bytes, *, user_id: uuid.UUID, role: str, ttl: int = 900) -> str:
    now = int(time.time())
    claims = {
        "sub": str(user_id),
        "role": role,
        "type": "access",
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + ttl,
    }
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": "dev-1"})


def _bearer(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


async def test_a_token_signed_by_the_issuer_resolves_to_the_current_user(
    private_pem: bytes,
) -> None:
    user_id = uuid.uuid4()
    token = _token(private_pem, user_id=user_id, role="instructor")

    current_user = await get_current_user(credentials=_bearer(token))

    assert current_user.id == user_id
    assert current_user.role == "instructor"


async def test_an_expired_token_is_rejected(private_pem: bytes) -> None:
    token = _token(private_pem, user_id=uuid.uuid4(), role="student", ttl=-60)

    with pytest.raises(UnauthorizedError):
        await get_current_user(credentials=_bearer(token))


async def test_missing_credentials_are_rejected(private_pem: bytes) -> None:
    with pytest.raises(UnauthorizedError):
        await get_current_user(credentials=None)


async def test_require_roles_rejects_a_non_matching_role() -> None:
    checker = require_roles("instructor")
    current_user = CurrentUser(id=uuid.uuid4(), role="student", jti="x", expires_at=0)

    with pytest.raises(ForbiddenError):
        await checker(current_user=current_user)
