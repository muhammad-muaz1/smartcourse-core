"""Requires Docker (testcontainers spins up a real Postgres).

get_session's commit/rollback only fires correctly when driven through FastAPI's own
dependency teardown (an AsyncExitStack that calls the equivalent of athrow() on
exception) — a raw ``async for session in get_session():`` in a test does *not*
reproduce that: an exception raised in the loop body silently orphans the generator
instead of triggering its except/finally blocks. Verified empirically before writing
this file. So these tests go through a real FastAPI app + TestClient, the same path
production code takes.
"""

from collections.abc import AsyncIterator

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from testcontainers.community.postgres import PostgresContainer

from src.config import PostgresSettings
from src.db import postgres
from src.db.base import Base, TimestampMixin, UUIDMixin


class _Widget(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "test_postgres_widgets"

    name: Mapped[str] = mapped_column()


@pytest.fixture
async def _postgres(postgres_container: PostgresContainer) -> AsyncIterator[None]:
    postgres.init_postgres(PostgresSettings(dsn=postgres_container.get_connection_url()))
    try:
        yield
    finally:
        await postgres.close_postgres()


@pytest.fixture
async def _widgets_table(_postgres: None) -> AsyncIterator[None]:
    engine = postgres._engine  # noqa: SLF001 - test-only access to set up/tear down a table
    assert engine is not None
    async with engine.begin() as conn:
        await conn.run_sync(_Widget.metadata.create_all)
    try:
        yield
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(_Widget.metadata.drop_all)


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.post("/widgets/{name}")
    async def create_widget(
        name: str, session: AsyncSession = Depends(postgres.get_session)
    ) -> dict[str, str]:
        session.add(_Widget(name=name))
        return {"name": name}

    @app.post("/widgets/{name}/boom")
    async def create_widget_then_fail(
        name: str, session: AsyncSession = Depends(postgres.get_session)
    ) -> None:
        session.add(_Widget(name=name))
        raise RuntimeError("boom")

    return app


async def test_check_succeeds_against_a_real_database(_postgres: None) -> None:
    await postgres.check()  # must not raise


async def test_session_dependency_commits_and_populates_defaults(_widgets_table: None) -> None:
    client = TestClient(_build_app())

    response = client.post("/widgets/course-101")
    assert response.status_code == 200

    async for session in postgres.get_session():
        result = await session.execute(select(_Widget).where(_Widget.name == "course-101"))
        widget = result.scalar_one()
        assert widget.id is not None
        assert widget.created_at is not None


async def test_session_dependency_rolls_back_when_the_route_raises(_widgets_table: None) -> None:
    client = TestClient(_build_app(), raise_server_exceptions=False)

    response = client.post("/widgets/never-committed/boom")
    assert response.status_code == 500

    async for session in postgres.get_session():
        result = await session.execute(select(_Widget).where(_Widget.name == "never-committed"))
        assert result.scalar_one_or_none() is None
