import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from platform_core.config import PostgresSettings
from platform_core.logging import clear_request_context
from src.db import (
    inbox,  # noqa: F401 - registers processed_events on Base.metadata
    postgres,
)
from src.db.base import Base
from src.modules.enrollments import models  # noqa: F401 - registers course_cache, enrollments


@pytest.fixture(autouse=True)
def _reset_request_context() -> None:
    clear_request_context()
    yield
    clear_request_context()


# Session-scoped so every test file shares one container instead of paying its own
# startup cost.
@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:16", driver="asyncpg") as container:
        yield container


@pytest.fixture
async def _db(postgres_container: PostgresContainer) -> AsyncIterator[None]:
    """A clean schema per test, built from the models (migrations are exercised by
    `alembic upgrade head` in CI and `make migrate`)."""
    postgres.init_postgres(PostgresSettings(dsn=postgres_container.get_connection_url()))
    engine = postgres.database.engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await postgres.close_postgres()


@pytest.fixture(scope="session")
def redis_container() -> Iterator[RedisContainer]:
    with RedisContainer("redis:7") as container:
        yield container


class TokenFactory:
    """Signs access tokens the way identity does, with a throwaway keypair, so route
    tests exercise the real offline verification."""

    def __init__(self, directory: Path) -> None:
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_key_path = directory / "public.pem"
        self.public_key_path.write_bytes(
            self._key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )

    def __call__(self, role: str, user_id: uuid.UUID | None = None) -> dict[str, str]:
        now = int(time.time())
        claims = {
            "sub": str(user_id or uuid.uuid4()),
            "role": role,
            "type": "access",
            "jti": str(uuid.uuid4()),
            "iat": now,
            "exp": now + 900,
        }
        token = jwt.encode(claims, self._key, algorithm="RS256", headers={"kid": "test"})
        return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def tokens(tmp_path: Path) -> TokenFactory:
    return TokenFactory(tmp_path)
