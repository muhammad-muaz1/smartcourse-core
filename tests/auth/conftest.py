from collections.abc import AsyncIterator

import pytest
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from src.config import PostgresSettings, RedisSettings
from src.db import postgres
from src.db import redis as redis_db
from src.db.base import Base


@pytest.fixture
async def db_and_redis(
    postgres_container: PostgresContainer, redis_container: RedisContainer
) -> AsyncIterator[None]:
    postgres.init_postgres(PostgresSettings(dsn=postgres_container.get_connection_url()))
    redis_url = f"redis://{redis_container.get_container_host_ip()}:{redis_container.get_exposed_port(6379)}/0"
    redis_db.init_redis(RedisSettings(url=redis_url))

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
        await redis_db.get_redis().flushdb()
        await redis_db.close_redis()
