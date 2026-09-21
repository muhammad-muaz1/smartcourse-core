"""Requires Docker (testcontainers spins up a real Redis)."""

from collections.abc import AsyncIterator

import pytest
from testcontainers.community.redis import RedisContainer

from src.config import RedisSettings
from src.db import redis as redis_db


@pytest.fixture
async def _redis(redis_container: RedisContainer) -> AsyncIterator[None]:
    url = f"redis://{redis_container.get_container_host_ip()}:{redis_container.get_exposed_port(6379)}/0"
    redis_db.init_redis(RedisSettings(url=url))
    try:
        yield
    finally:
        await redis_db.close_redis()


async def test_check_succeeds_against_a_real_redis(_redis: None) -> None:
    await redis_db.check()  # must not raise


async def test_get_redis_returns_a_usable_client(_redis: None) -> None:
    client = redis_db.get_redis()
    await client.set("k", "v")
    assert await client.get("k") == "v"
