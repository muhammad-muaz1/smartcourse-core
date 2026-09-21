"""Async engine, session factory, get_session dependency, check() for health.

One transaction per request: get_session commits on a clean exit and rolls back on
exception. Services never call commit/rollback themselves — they raise or return.

connect_args sets asyncpg's own connect timeout to 5s instead of its 60s default — the
same reasoning as Mongo's serverSelectionTimeoutMS (docs/adr/0005): an unreachable
Postgres must fail fast, not hang every connection attempt for a minute.
"""

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config import PostgresSettings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_postgres(settings: PostgresSettings) -> None:
    global _engine, _session_factory
    _engine = create_async_engine(
        settings.dsn,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        echo=settings.echo,
        connect_args={"timeout": 5},
    )
    _session_factory = async_sessionmaker(bind=_engine, expire_on_commit=False)


async def close_postgres() -> None:
    if _engine is not None:
        await _engine.dispose()


async def get_session() -> AsyncIterator[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("init_postgres() must run before get_session() is used")
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check() -> None:
    if _engine is None:
        raise RuntimeError("init_postgres() must run before check() is used")
    async with _engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
