"""Test infrastructure every suite shares: a real Postgres and Redis per test session.

By default each is a testcontainers container (needs Docker). On a machine without
Docker, ``make test-local`` starts a throwaway Postgres cluster and Redis under .tools/
and exports their URLs; the fixtures then use those instead. Never point these at a
development database or Redis — the suites drop every table and FLUSHDB between tests.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol
from urllib.parse import urlparse

POSTGRES_URL_ENV = "SMARTCOURSE_TEST_POSTGRES_URL"
REDIS_URL_ENV = "SMARTCOURSE_TEST_REDIS_URL"


class PostgresLike(Protocol):
    def get_connection_url(self) -> str: ...


class RedisLike(Protocol):
    def get_container_host_ip(self) -> str: ...

    def get_exposed_port(self, port: int) -> int: ...


class _External:
    """Quacks like the testcontainers objects the fixtures already use."""

    def __init__(self, url: str) -> None:
        self._url = url
        parsed = urlparse(url)
        self._host = parsed.hostname or "localhost"
        self._port = parsed.port or 0

    def get_connection_url(self) -> str:
        return self._url

    def get_container_host_ip(self) -> str:
        return self._host

    def get_exposed_port(self, _port: int) -> int:
        return self._port


@contextmanager
def throwaway_postgres() -> Iterator[PostgresLike]:
    url = os.environ.get(POSTGRES_URL_ENV)
    if url:
        yield _External(url)
        return
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:16", driver="asyncpg") as container:
        yield container


@contextmanager
def throwaway_redis() -> Iterator[RedisLike]:
    url = os.environ.get(REDIS_URL_ENV)
    if url:
        yield _External(url)
        return
    from testcontainers.community.redis import RedisContainer

    with RedisContainer("redis:7") as container:
        yield container
