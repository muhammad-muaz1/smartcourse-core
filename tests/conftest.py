from collections.abc import Iterator

import pytest
from testcontainers.community.mongodb import MongoDbContainer
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from src.core.logging import clear_request_context


@pytest.fixture(autouse=True)
def _reset_request_context() -> None:
    clear_request_context()
    yield
    clear_request_context()


# Session-scoped so tests/db/ and tests/health/ share one container each instead of
# every test file paying its own startup cost.


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:16", driver="asyncpg") as container:
        yield container


@pytest.fixture(scope="session")
def mongo_container() -> Iterator[MongoDbContainer]:
    with MongoDbContainer("mongo:7") as container:
        yield container


@pytest.fixture(scope="session")
def redis_container() -> Iterator[RedisContainer]:
    with RedisContainer("redis:7") as container:
        yield container
