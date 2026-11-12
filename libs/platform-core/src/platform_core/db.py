"""Connection mechanics for Postgres and Redis, plus the column mixins every model uses.

Each service owns its own ``Base`` (its own metadata, its own database) and its own
connection instances in ``src/db/`` — this module only supplies the machinery, so a
connection-handling fix lands in every service at once.

One transaction per request: ``get_session`` commits on a clean exit and rolls back on
exception. Services never call commit/rollback themselves — they raise or return.

asyncpg's connect timeout is 5s instead of its 60s default: an unreachable Postgres
must fail fast, not hang every connection attempt for a minute (docs/adr/0005).
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import DateTime, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.config import PostgresSettings, RedisSettings

# Alembic's own recommended naming convention — without it, autogenerate produces
# constraint names that vary by dialect and are unreviewable in a migration diff.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class UUIDMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    # timezone=True is not optional: timezone-aware UTC everywhere, and the default
    # SQLAlchemy DateTime column is naive — asyncpg rejects a tz-aware Python datetime
    # against a naive column outright, so this is the difference between working and
    # a 500.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(tz=UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(tz=UTC),
        onupdate=lambda: datetime.now(tz=UTC),
    )


class PostgresDatabase:
    def __init__(self) -> None:
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    def init(self, settings: PostgresSettings) -> None:
        self._engine = create_async_engine(
            settings.dsn,
            pool_size=settings.pool_size,
            max_overflow=settings.max_overflow,
            echo=settings.echo,
            connect_args={"timeout": 5},
        )
        self._session_factory = async_sessionmaker(bind=self._engine, expire_on_commit=False)

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("init() must run before the engine is used")
        return self._engine

    async def get_session(self) -> AsyncIterator[AsyncSession]:
        if self._session_factory is None:
            raise RuntimeError("init() must run before get_session() is used")
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    @asynccontextmanager
    async def session_scope(self) -> AsyncIterator[AsyncSession]:
        """One transaction outside an HTTP request — a Temporal activity or a relay
        loop. Same contract as ``get_session``: commit on clean exit, roll back on
        exception; the service functions it is passed to still never commit."""
        async for session in self.get_session():
            yield session

    async def check(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))


class RedisConnection:
    def __init__(self) -> None:
        self._client: Redis | None = None

    def init(self, settings: RedisSettings) -> None:
        self._client = Redis.from_url(
            settings.url,
            socket_timeout=settings.socket_timeout_seconds,
            socket_connect_timeout=settings.socket_timeout_seconds,
            decode_responses=True,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def get_redis(self) -> Redis:
        if self._client is None:
            raise RuntimeError("init() must run before get_redis() is used")
        return self._client

    async def check(self) -> None:
        await self.get_redis().ping()
