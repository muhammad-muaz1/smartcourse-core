from collections.abc import Iterator

import pytest
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from platform_core.logging import clear_request_context
from platform_core.testing import throwaway_postgres, throwaway_redis


@pytest.fixture(autouse=True)
def _reset_request_context() -> None:
    clear_request_context()
    yield
    clear_request_context()


# Session-scoped so every test file shares one container each instead of paying its own
# startup cost.


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    with throwaway_postgres() as container:  # Docker, or SMARTCOURSE_TEST_POSTGRES_URL
        yield container


@pytest.fixture(scope="session")
def redis_container() -> Iterator[RedisContainer]:
    with throwaway_redis() as container:  # Docker, or SMARTCOURSE_TEST_REDIS_URL
        yield container
