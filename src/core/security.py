"""Password hashing + JWT encode/decode. Nothing here touches a database or FastAPI —
it's pure crypto/claims, callable from a service, a script, or a test alike.
"""

import base64
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

from src.config import SecuritySettings

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(*, password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


@lru_cache
def _load_private_key(path: str) -> RSAPrivateKey:
    key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(key, RSAPrivateKey):
        raise TypeError(f"{path} is not an RSA private key")
    return key


@lru_cache
def _load_public_key(path: str) -> RSAPublicKey:
    key = serialization.load_pem_public_key(Path(path).read_bytes())
    if not isinstance(key, RSAPublicKey):
        raise TypeError(f"{path} is not an RSA public key")
    return key


TokenType = Literal["access"]


class InvalidTokenError(Exception):
    pass


def create_access_token(*, user_id: uuid.UUID, role: str, settings: SecuritySettings) -> str:
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "role": role,
        "type": "access",
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + settings.access_token_expires_minutes * 60,
    }
    private_key = _load_private_key(settings.jwt_private_key_path)
    return jwt.encode(
        payload, private_key, algorithm=settings.jwt_algorithm, headers={"kid": settings.jwt_kid}
    )


def decode_access_token(token: str, *, settings: SecuritySettings) -> dict[str, Any]:
    public_key = _load_public_key(settings.jwt_public_key_path)
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


def _b64url_uint(value: int) -> str:
    byte_length = (value.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(value.to_bytes(byte_length, "big")).rstrip(b"=").decode("ascii")


def access_token_denylist_key(jti: str) -> str:
    """Shared between core/dependencies.py (reads it on every request) and
    modules/auth/service.py (writes it on logout) — one place for the key format."""
    return f"auth:denylist:{jti}"


def build_jwks(settings: SecuritySettings) -> dict[str, Any]:
    public_key = _load_public_key(settings.jwt_public_key_path)
    numbers = public_key.public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": settings.jwt_algorithm,
                "kid": settings.jwt_kid,
                "n": _b64url_uint(numbers.n),
                "e": _b64url_uint(numbers.e),
            }
        ]
    }
