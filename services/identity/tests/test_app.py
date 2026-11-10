from fastapi.testclient import TestClient
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from src.app import create_app
from src.config import (
    IdentityPostgresSettings,
    IdentityRedisSettings,
    SecuritySettings,
    Settings,
)

# Port 1 is reserved and never has anything listening, so a connection there fails
# fast with "connection refused" regardless of what a developer happens to have
# running locally on the real default ports — this test must not depend on the
# ambient state of whoever's machine it runs on.
_UNREACHABLE_PORT = 1


def _client_with_everything_unreachable(security: SecuritySettings | None = None) -> TestClient:
    settings = Settings(
        _env_file=None,
        postgres=IdentityPostgresSettings(
            dsn=f"postgresql+asyncpg://postgres:postgres@localhost:{_UNREACHABLE_PORT}/identity_db"
        ),
        redis=IdentityRedisSettings(url=f"redis://localhost:{_UNREACHABLE_PORT}/0"),
        security=security or SecuritySettings(),
    )
    return TestClient(create_app(settings))


def test_health_returns_503_and_names_each_unhealthy_dependency() -> None:
    with _client_with_everything_unreachable() as client:
        response = client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    assert body["service"] == "identity"
    assert set(body["services"]) == {"postgres", "redis"}
    assert all(s["status"] == "error" for s in body["services"].values())


def test_version_works_even_when_everything_else_is_down() -> None:
    with _client_with_everything_unreachable() as client:
        response = client.get("/version")

    assert response.status_code == 200
    assert response.json()["service"] == "identity"


def test_auth_routes_are_mounted_under_the_api_prefix() -> None:
    with _client_with_everything_unreachable() as client:
        paths = client.get("/openapi.json").json()["paths"]

    assert "/api/v1/auth/login" in paths
    assert "/health" in paths


def test_jwks_is_served_at_the_host_root(security_settings: SecuritySettings) -> None:
    with _client_with_everything_unreachable(security_settings) as client:
        response = client.get("/.well-known/jwks.json")

    assert response.status_code == 200
    assert response.json()["keys"][0]["kid"] == "test-kid"


def test_health_returns_200_when_every_dependency_is_reachable(
    postgres_container: PostgresContainer, redis_container: RedisContainer
) -> None:
    """Requires Docker (testcontainers spins up real Postgres and Redis)."""
    settings = Settings(
        _env_file=None,
        postgres=IdentityPostgresSettings(dsn=postgres_container.get_connection_url()),
        redis=IdentityRedisSettings(
            url=f"redis://{redis_container.get_container_host_ip()}:"
            f"{redis_container.get_exposed_port(6379)}/0"
        ),
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert all(s["status"] == "ok" for s in body["services"].values())
