"""Pure unit tests — no database, no network. security_settings (tests/core/conftest.py)
generates a throwaway RSA keypair per test rather than depending on `make keys` having
been run.
"""

import time
import uuid
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.config import SecuritySettings
from src.core.security import (
    InvalidTokenError,
    access_token_denylist_key,
    build_jwks,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def test_hash_password_is_not_the_plaintext() -> None:
    hashed = hash_password("correcthorse123")
    assert hashed != "correcthorse123"
    assert hashed.startswith("$argon2id$")


def test_verify_password_accepts_the_right_password() -> None:
    hashed = hash_password("correcthorse123")
    assert verify_password(password="correcthorse123", password_hash=hashed) is True


def test_verify_password_rejects_the_wrong_password() -> None:
    hashed = hash_password("correcthorse123")
    assert verify_password(password="wrong", password_hash=hashed) is False


def test_verify_password_rejects_a_garbage_hash_instead_of_raising() -> None:
    assert verify_password(password="anything", password_hash="not-a-real-hash") is False


def test_access_token_round_trips(security_settings: SecuritySettings) -> None:
    user_id = uuid.uuid4()
    token = create_access_token(user_id=user_id, role="student", settings=security_settings)

    payload = decode_access_token(token, settings=security_settings)

    assert payload["sub"] == str(user_id)
    assert payload["role"] == "student"
    assert payload["type"] == "access"
    assert "jti" in payload


def test_access_token_carries_the_configured_kid(security_settings: SecuritySettings) -> None:
    token = create_access_token(user_id=uuid.uuid4(), role="student", settings=security_settings)
    assert jwt.get_unverified_header(token)["kid"] == "test-kid"


def test_expired_access_token_is_rejected(security_settings: SecuritySettings) -> None:
    expired = SecuritySettings(
        jwt_private_key_path=security_settings.jwt_private_key_path,
        jwt_public_key_path=security_settings.jwt_public_key_path,
        jwt_kid=security_settings.jwt_kid,
        access_token_expires_minutes=-1,
    )
    token = create_access_token(user_id=uuid.uuid4(), role="student", settings=expired)

    with pytest.raises(InvalidTokenError):
        decode_access_token(token, settings=security_settings)


def test_token_signed_with_a_different_key_is_rejected(
    security_settings: SecuritySettings, tmp_path: Path
) -> None:
    other_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pem = other_private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    now = int(time.time())
    forged = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "role": "admin",
            "type": "access",
            "jti": str(uuid.uuid4()),
            "iat": now,
            "exp": now + 900,
        },
        other_pem,
        algorithm="RS256",
        headers={"kid": security_settings.jwt_kid},
    )

    with pytest.raises(InvalidTokenError):
        decode_access_token(forged, settings=security_settings)


def test_build_jwks_exposes_the_public_key_with_the_configured_kid(
    security_settings: SecuritySettings,
) -> None:
    jwks = build_jwks(security_settings)

    assert len(jwks["keys"]) == 1
    key = jwks["keys"][0]
    assert key["kid"] == "test-kid"
    assert key["kty"] == "RSA"
    assert key["alg"] == "RS256"
    assert key["e"] == "AQAB"


def test_access_token_denylist_key_is_namespaced() -> None:
    assert access_token_denylist_key("abc") == "auth:denylist:abc"
