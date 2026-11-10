from fastapi.testclient import TestClient

from src.app import create_app
from src.config import CoursePostgresSettings, Settings

# Port 1 is reserved and never has anything listening, so a connection there fails
# fast regardless of what happens to be running on the developer's machine.
_UNREACHABLE_PORT = 1


def _client() -> TestClient:
    settings = Settings(
        _env_file=None,
        postgres=CoursePostgresSettings(
            dsn=f"postgresql+asyncpg://postgres:postgres@localhost:{_UNREACHABLE_PORT}/course_db"
        ),
    )
    return TestClient(create_app(settings))


def test_health_reports_this_service_and_its_own_database_only() -> None:
    with _client() as client:
        response = client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["service"] == "course"
    assert set(body["services"]) == {"postgres"}


def test_course_routes_are_mounted_under_the_api_prefix() -> None:
    with _client() as client:
        paths = client.get("/openapi.json").json()["paths"]

    assert "/api/v1/courses" in paths
    assert "/api/v1/courses/{course_id}" in paths


def test_creating_a_course_without_a_token_is_unauthorized() -> None:
    with _client() as client:
        response = client.post(
            "/api/v1/courses", json={"title": "t", "description": "d", "level": "beginner"}
        )

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"
