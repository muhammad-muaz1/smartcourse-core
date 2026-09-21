from fastapi.testclient import TestClient
from testcontainers.community.mongodb import MongoDbContainer
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from src.app import create_app
from src.config import MongoSettings, PostgresSettings, RedisSettings, Settings

# Port 1 is reserved and never has anything listening, so a connection there fails
# fast with "connection refused" regardless of what a developer happens to have
# running locally on the real default ports — this test must not depend on the
# ambient state of whoever's machine it runs on.
_UNREACHABLE_PORT = 1


def _client_with_everything_unreachable() -> TestClient:
    settings = Settings(
        _env_file=None,
        postgres=PostgresSettings(
            dsn=f"postgresql+asyncpg://postgres:postgres@localhost:{_UNREACHABLE_PORT}/smartcourse"
        ),
        mongo=MongoSettings(
            url=f"mongodb://localhost:{_UNREACHABLE_PORT}", server_selection_timeout_ms=200
        ),
        redis=RedisSettings(url=f"redis://localhost:{_UNREACHABLE_PORT}/0"),
    )
    return TestClient(create_app(settings))


def test_health_returns_503_and_names_each_unhealthy_dependency() -> None:
    with _client_with_everything_unreachable() as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    assert set(body["services"]) == {"postgres", "mongo", "redis"}
    assert all(s["status"] == "error" for s in body["services"].values())


def test_version_works_even_when_everything_else_is_down() -> None:
    with _client_with_everything_unreachable() as client:
        response = client.get("/api/v1/version")

    assert response.status_code == 200
    assert "version" in response.json()


def test_health_returns_200_when_every_dependency_is_reachable(
    postgres_container: PostgresContainer,
    mongo_container: MongoDbContainer,
    redis_container: RedisContainer,
) -> None:
    """Requires Docker (testcontainers spins up real Postgres, Mongo and Redis)."""
    settings = Settings(
        _env_file=None,
        postgres=PostgresSettings(dsn=postgres_container.get_connection_url()),
        mongo=MongoSettings(
            url=mongo_container.get_connection_url(), database="smartcourse_health"
        ),
        redis=RedisSettings(
            url=f"redis://{redis_container.get_container_host_ip()}:"
            f"{redis_container.get_exposed_port(6379)}/0"
        ),
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert all(s["status"] == "ok" for s in body["services"].values())
