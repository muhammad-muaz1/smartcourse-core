"""Who may see which metric, the as_of contract, and the cache — over HTTP with real
token verification. Requires Docker or SMARTCOURSE_TEST_POSTGRES_URL / _REDIS_URL."""

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from platform_core.config import JWTVerifySettings
from src.app import create_app
from src.config import AnalyticsPostgresSettings, AnalyticsRedisSettings, Settings
from tests.conftest import TokenFactory
from tests.metrics.helpers import apply, course, enrollment


@pytest.fixture
def client(
    _db: None,
    postgres_container: PostgresContainer,
    redis_container: RedisContainer,
    tokens: TokenFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(6379)
    settings = Settings(
        _env_file=None,
        postgres=AnalyticsPostgresSettings(dsn=postgres_container.get_connection_url()),
        redis=AnalyticsRedisSettings(url=f"redis://{host}:{port}/5"),
        security=JWTVerifySettings(jwt_public_key_path=str(tokens.public_key_path)),
    )
    monkeypatch.setattr("src.core.dependencies.get_settings", lambda: settings)
    with TestClient(create_app(settings)) as test_client:
        assert test_client.portal is not None
        test_client.portal.call(_flush_cache, test_client)
        yield test_client


async def _flush_cache(_client: TestClient) -> None:
    from src.db import redis

    await redis.get_redis().flushdb()


def _seed(client: TestClient, *events: object) -> None:
    assert client.portal is not None
    client.portal.call(apply, *events)


def test_projection_reads_carry_as_of(client: TestClient, tokens: TokenFactory) -> None:
    response = client.get("/api/v1/analytics/overview", headers=tokens("admin"))
    assert response.status_code == 200
    assert "as_of" in response.json()["meta"]


def test_platform_wide_figures_are_admin_only(client: TestClient, tokens: TokenFactory) -> None:
    student, instructor = tokens("student"), tokens("instructor")
    assert client.get("/api/v1/analytics/overview", headers=student).status_code == 403
    assert (
        client.get("/api/v1/analytics/students/course-load", headers=instructor).status_code == 403
    )
    assert client.get("/api/v1/analytics/failures", headers=instructor).status_code == 403
    assert client.get("/api/v1/analytics/completion", headers=instructor).status_code == 403
    # Anyone signed in may see what's popular.
    assert client.get("/api/v1/analytics/courses/popular", headers=student).status_code == 200


def test_an_instructor_sees_their_own_course_only(client: TestClient, tokens: TokenFactory) -> None:
    owner_id, course_id = uuid.uuid4(), uuid.uuid4()
    _seed(client, course(course_id, instructor_id=owner_id), enrollment(uuid.uuid4(), course_id))
    owner, other = tokens("instructor", owner_id), tokens("instructor")

    mine = client.get(f"/api/v1/analytics/courses/{course_id}", headers=owner)
    assert mine.status_code == 200 and mine.json()["data"]["enrollments"] == 1
    assert client.get(f"/api/v1/analytics/courses/{course_id}", headers=other).status_code == 403
    scoped = client.get(f"/api/v1/analytics/enrollments?course_id={course_id}", headers=owner)
    assert scoped.status_code == 200
    unknown = client.get(f"/api/v1/analytics/completion?course_id={uuid.uuid4()}", headers=owner)
    assert (unknown.status_code, unknown.json()["code"]) == (404, "COURSE_NOT_FOUND")


def test_results_are_cached_for_their_ttl(client: TestClient, tokens: TokenFactory) -> None:
    admin = tokens("admin")
    before = client.get("/api/v1/analytics/time-to-complete", headers=admin).json()["data"]
    course_id = uuid.uuid4()
    _seed(
        client, course(course_id, instructor_id=uuid.uuid4()), enrollment(uuid.uuid4(), course_id)
    )
    after = client.get("/api/v1/analytics/time-to-complete", headers=admin).json()["data"]
    assert after == before  # served from Redis until the TTL lapses


def test_a_backwards_range_is_rejected(client: TestClient, tokens: TokenFactory) -> None:
    response = client.get(
        "/api/v1/analytics/enrollments?from=2026-02-01T00:00:00Z&to=2026-01-01T00:00:00Z",
        headers=tokens("admin"),
    )
    assert response.status_code == 422
