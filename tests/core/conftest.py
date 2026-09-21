from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.config import SecuritySettings


@pytest.fixture
def security_settings(tmp_path: Path) -> SecuritySettings:
    """A throwaway RSA keypair per test, so these tests never depend on `make keys`
    having been run against the real secrets/ directory."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_path = tmp_path / "private.pem"
    public_path = tmp_path / "public.pem"
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return SecuritySettings(
        jwt_private_key_path=str(private_path),
        jwt_public_key_path=str(public_path),
        jwt_kid="test-kid",
    )
