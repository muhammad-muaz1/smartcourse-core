"""Generates a local dev RSA keypair for JWT signing, if one doesn't already exist.
Run via `make keys`. Never overwrites existing keys — delete secrets/ yourself first if
you want new ones (that invalidates every outstanding token, on purpose).
"""

from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.config import get_settings


def main() -> None:
    settings = get_settings().security
    private_path = Path(settings.jwt_private_key_path)
    public_path = Path(settings.jwt_public_key_path)

    if private_path.exists() and public_path.exists():
        print(f"Keys already exist at {private_path} / {public_path} — leaving them alone.")
        return

    private_path.parent.mkdir(parents=True, exist_ok=True)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

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
    private_path.chmod(0o600)
    print(f"Generated dev JWT keypair: {private_path} / {public_path}")


if __name__ == "__main__":
    main()
