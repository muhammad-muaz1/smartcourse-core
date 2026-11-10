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
from sqlalchemy import MetaData, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from testcontainers.community.postgres import PostgresContainer

from platform_core.config import PostgresSettings
from platform_core.db import NAMING_CONVENTION, PostgresDatabase, TimestampMixin, UUIDMixin


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


postgres = PostgresDatabase()


class _Widget(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "test_postgres_widgets"

    name: Mapped[str] = mapped_column()


@pytest.fixture
async def _postgres(postgres_container: PostgresContainer) -> AsyncIterator[None]:
    postgres.init(PostgresSettings(dsn=postgres_container.get_connection_url()))
    try:
        yield
    finally:
        await postgres.close()


@pytest.fixture
async def _widgets_table(_postgres: None) -> AsyncIterator[None]:
    engine = postgres.engine
    async with engine.begin() as conn:
        await conn.run_sync(_Widget.metadata.create_all)
    await _drop_pooled_connections()
    try:
        yield
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(_Widget.metadata.drop_all)


async def _drop_pooled_connections() -> None:
    """TestClient runs the app on its own event loop, and an asyncpg connection only
    works on the loop that opened it. Emptying the pool at each hand-over between the
    test's loop and TestClient's keeps either side from being handed the other's
    connection. close=False: the other loop may already be gone, so its connections are
    dereferenced rather than closed on this one."""
    await postgres.engine.dispose(close=False)


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
    await _drop_pooled_connections()

    async for session in postgres.get_session():
        result = await session.execute(select(_Widget).where(_Widget.name == "course-101"))
        widget = result.scalar_one()
        assert widget.id is not None
        assert widget.created_at is not None


async def test_session_dependency_rolls_back_when_the_route_raises(_widgets_table: None) -> None:
    client = TestClient(_build_app(), raise_server_exceptions=False)

    response = client.post("/widgets/never-committed/boom")
    assert response.status_code == 500
    await _drop_pooled_connections()

    async for session in postgres.get_session():
        result = await session.execute(select(_Widget).where(_Widget.name == "never-committed"))
        assert result.scalar_one_or_none() is None
