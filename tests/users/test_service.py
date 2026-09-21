"""Requires Docker (testcontainers spins up a real Postgres)."""

import uuid
from collections.abc import AsyncIterator

import pytest
from testcontainers.community.postgres import PostgresContainer

from src.config import PostgresSettings
from src.db import postgres
from src.db.base import Base
from src.modules.users import service
from src.modules.users.models import UserRole


@pytest.fixture
async def _db(postgres_container: PostgresContainer) -> AsyncIterator[None]:
    postgres.init_postgres(PostgresSettings(dsn=postgres_container.get_connection_url()))
    engine = postgres._engine  # noqa: SLF001 - test-only setup/teardown
    assert engine is not None
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await postgres.close_postgres()


async def test_create_user_then_get_by_email(_db: None) -> None:
    async for session in postgres.get_session():
        user = await service.create_user(
            session,
            email="new@example.com",
            hashed_password="hashed",
            full_name="New User",
            role=UserRole.STUDENT,
        )
        assert user.id is not None

    async for session in postgres.get_session():
        found = await service.get_by_email(session, "new@example.com")
        assert found is not None
        assert found.full_name == "New User"
        assert found.role == UserRole.STUDENT


async def test_get_by_email_returns_none_when_missing(_db: None) -> None:
    async for session in postgres.get_session():
        assert await service.get_by_email(session, "nobody@example.com") is None


async def test_get_by_id_returns_none_when_missing(_db: None) -> None:
    async for session in postgres.get_session():
        assert await service.get_by_id(session, uuid.uuid4()) is None


async def test_get_by_id_round_trips(_db: None) -> None:
    async for session in postgres.get_session():
        created = await service.create_user(
            session,
            email="byid@example.com",
            hashed_password="hashed",
            full_name="By Id",
            role=UserRole.INSTRUCTOR,
        )
        user_id = created.id

    async for session in postgres.get_session():
        found = await service.get_by_id(session, user_id)
        assert found is not None
        assert found.email == "byid@example.com"
