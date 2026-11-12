from fastapi.testclient import TestClient

from src.app import create_app
from src.config import LearningPostgresSettings, LearningRedisSettings, Settings

# Port 1 is reserved and never has anything listening, so a connection there fails
# fast regardless of what happens to be running on the developer's machine.
_UNREACHABLE_PORT = 1


def _client() -> TestClient:
    settings = Settings(
        _env_file=None,
        postgres=LearningPostgresSettings(
            dsn=f"postgresql+asyncpg://postgres:postgres@localhost:{_UNREACHABLE_PORT}/learning_db"
        ),
        redis=LearningRedisSettings(url=f"redis://localhost:{_UNREACHABLE_PORT}/3"),
    )
    return TestClient(create_app(settings))


def test_health_reports_this_service_and_its_own_dependencies_only() -> None:
    with _client() as client:
        response = client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["service"] == "learning"
    assert set(body["services"]) == {"postgres", "redis"}


def test_enrollment_routes_are_mounted_under_the_api_prefix() -> None:
    with _client() as client:
        paths = client.get("/openapi.json").json()["paths"]

    assert "/api/v1/courses/{course_id}/enrollments" in paths
    assert "/api/v1/enrollments/me" in paths
    assert "/api/v1/enrollments/{enrollment_id}" in paths
    assert "/api/v1/users/{user_id}/enrollment-history" in paths


def test_enrolling_without_a_token_is_unauthorized() -> None:
    with _client() as client:
        response = client.post("/api/v1/courses/3f6c1c1e-0000-4000-8000-000000000000/enrollments")

    assert response.status_code == 401
