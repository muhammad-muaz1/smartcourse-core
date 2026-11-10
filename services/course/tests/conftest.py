from collections.abc import Iterator

import pytest
from testcontainers.community.postgres import PostgresContainer

from platform_core.logging import clear_request_context


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
