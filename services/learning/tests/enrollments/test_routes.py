"""The enrollment HTTP surface with real token verification, Postgres and Redis.
Requires Docker (testcontainers)."""

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from platform_core.config import JWTVerifySettings
from src.app import create_app
from src.config import LearningPostgresSettings, LearningRedisSettings, Settings
from src.modules.enrollments.activities import apply_course_event
from tests.conftest import TokenFactory
from tests.enrollments.helpers import course_event


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
        postgres=LearningPostgresSettings(dsn=postgres_container.get_connection_url()),
        redis=LearningRedisSettings(url=f"redis://{host}:{port}/3"),
        security=JWTVerifySettings(jwt_public_key_path=str(tokens.public_key_path)),
    )
    # Auth dependencies read the process-wide settings, not the app's.
    monkeypatch.setattr("src.core.dependencies.get_settings", lambda: settings)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _published_course(client: TestClient, instructor: uuid.UUID, **kwargs: object) -> uuid.UUID:
    course_id = uuid.uuid4()
    event = course_event(course_id, revision=1, instructor_id=instructor, **kwargs)  # type: ignore[arg-type]
    assert client.portal is not None
    client.portal.call(apply_course_event, event)
    return course_id


def _key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def test_enrolment_flow(client: TestClient, tokens: TokenFactory) -> None:
    instructor_id, student_id = uuid.uuid4(), uuid.uuid4()
    course_id = _published_course(client, instructor_id, max_enrollments=1)
    student = tokens("student", student_id)
    url = f"/api/v1/courses/{course_id}/enrollments"

    assert client.post(url, headers=student).json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert client.post(url, headers=tokens("instructor") | _key()).status_code == 403

    key = _key()
    first = client.post(url, headers=student | key)
    retry = client.post(url, headers=student | key)
    assert first.status_code == retry.status_code == 201
    assert retry.json()["data"] == first.json()["data"]
    assert retry.headers["idempotent-replayed"] == "true"
    enrollment_id = first.json()["data"]["id"]

    duplicate = client.post(url, headers=student | _key())
    assert (duplicate.status_code, duplicate.json()["code"]) == (409, "ALREADY_ENROLLED")
    full = client.post(url, headers=tokens("student") | _key())
    assert (full.status_code, full.json()["code"]) == (409, "COURSE_FULL")

    mine = client.get("/api/v1/enrollments/me", headers=student).json()["data"]
    assert [e["id"] for e in mine["items"]] == [enrollment_id]
    owner = tokens("instructor", instructor_id)
    assert client.get(f"/api/v1/enrollments/{enrollment_id}", headers=owner).status_code == 200
    roster = client.get(url, headers=owner).json()["data"]
    assert roster["items"][0]["student_id"] == str(student_id)

    assert client.delete(f"/api/v1/enrollments/{enrollment_id}", headers=student).status_code == 204
    history = client.get(f"/api/v1/users/{student_id}/enrollment-history", headers=student)
    assert history.json()["data"]["items"][0]["status"] == "withdrawn"
    withdrawn = client.get("/api/v1/enrollments/me?status=withdrawn", headers=student).json()[
        "data"
    ]
    assert len(withdrawn["items"]) == 1


def test_a_course_that_is_not_published_is_not_enrollable(
    client: TestClient, tokens: TokenFactory
) -> None:
    course_id = _published_course(client, uuid.uuid4(), status="draft")
    response = client.post(
        f"/api/v1/courses/{course_id}/enrollments", headers=tokens("student") | _key()
    )
    assert (response.status_code, response.json()["code"]) == (422, "COURSE_NOT_ENROLLABLE")
