"""The HTTP surface end to end: real app, real token verification, real Postgres and
Redis. Requires Docker (testcontainers)."""

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from platform_core.config import JWTVerifySettings
from src.app import create_app
from src.config import CoursePostgresSettings, CourseRedisSettings, Settings
from src.db import postgres
from src.modules.courses.service import publishing
from tests.conftest import TokenFactory


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
        postgres=CoursePostgresSettings(dsn=postgres_container.get_connection_url()),
        redis=CourseRedisSettings(url=f"redis://{host}:{port}/1"),
        security=JWTVerifySettings(jwt_public_key_path=str(tokens.public_key_path)),
    )
    # Auth dependencies read the process-wide settings, not the app's.
    monkeypatch.setattr("src.core.dependencies.get_settings", lambda: settings)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _build_course(client: TestClient, owner: dict[str, str]) -> tuple[str, str, str]:
    course = client.post(
        "/api/v1/courses",
        headers=owner,
        json={"title": "DS", "description": "d", "level": "advanced", "max_enrollments": 5},
    ).json()["data"]
    module = client.post(
        f"/api/v1/courses/{course['id']}/modules", headers=owner, json={"title": "M1"}
    ).json()["data"]
    lesson = client.post(
        f"/api/v1/modules/{module['id']}/lessons",
        headers=owner,
        json={"title": "L1", "lesson_type": "text", "body": {"content": "hi"}},
    ).json()["data"]
    return course["id"], module["id"], lesson["id"]


def test_content_structure_round_trip(client: TestClient, tokens: TokenFactory) -> None:
    owner = tokens("instructor")
    course_id, module_id, lesson_id = _build_course(client, owner)

    second = client.post(
        f"/api/v1/courses/{course_id}/modules", headers=owner, json={"title": "M2"}
    )
    assert second.status_code == 201
    assert second.json()["data"]["lessons"] == []
    order = client.put(
        f"/api/v1/courses/{course_id}/modules/order",
        headers=owner,
        json={"module_ids": [second.json()["data"]["id"], module_id]},
    )
    assert [m["title"] for m in order.json()["data"]] == ["M2", "M1"]

    patched = client.patch(f"/api/v1/modules/{module_id}", headers=owner, json={"title": "Intro"})
    assert patched.json()["data"]["title"] == "Intro"
    lesson = client.patch(
        f"/api/v1/lessons/{lesson_id}",
        headers=owner,
        json={"lesson_type": "video", "body": {"video_url": "https://example.com/v.mp4"}},
    )
    assert lesson.json()["data"]["lesson_type"] == "video"
    assert client.get(f"/api/v1/lessons/{lesson_id}", headers=owner).status_code == 200
    lessons = client.put(
        f"/api/v1/modules/{module_id}/lessons/order",
        headers=owner,
        json={"lesson_ids": [lesson_id]},
    )
    assert [x["id"] for x in lessons.json()["data"]] == [lesson_id]

    detail = client.get(f"/api/v1/courses/{course_id}").json()["data"]
    assert [m["title"] for m in detail["modules"]] == ["M2", "Intro"]
    assert detail["modules"][1]["lessons"][0]["lesson_type"] == "video"

    assert client.delete(f"/api/v1/lessons/{lesson_id}", headers=owner).status_code == 204
    assert client.delete(f"/api/v1/modules/{module_id}", headers=owner).status_code == 204
    modules = client.get(f"/api/v1/courses/{course_id}/modules").json()["data"]
    assert [(m["title"], m["position"]) for m in modules] == [("M2", 0)]


def test_publish_is_idempotent_and_exposes_the_run(
    client: TestClient, tokens: TokenFactory
) -> None:
    owner = tokens("instructor")
    course_id, _, _ = _build_course(client, owner)

    missing = client.post(f"/api/v1/courses/{course_id}/publish", headers=owner)
    assert missing.status_code == 400
    assert missing.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    key = _key()
    first = client.post(f"/api/v1/courses/{course_id}/publish", headers=owner | key)
    retry = client.post(f"/api/v1/courses/{course_id}/publish", headers=owner | key)
    assert first.status_code == retry.status_code == 202
    run = first.json()["data"]
    assert retry.json()["data"]["id"] == run["id"]
    assert retry.headers["idempotent-replayed"] == "true"
    assert first.headers["location"] == f"/api/v1/courses/{course_id}/publish-runs/{run['id']}"

    again = client.post(f"/api/v1/courses/{course_id}/publish", headers=owner | _key())
    assert again.status_code == 409
    assert again.json()["code"] == "INVALID_COURSE_STATE"
    locked = client.post(f"/api/v1/courses/{course_id}/modules", headers=owner, json={"title": "x"})
    assert locked.json()["code"] == "COURSE_NOT_EDITABLE"

    runs = client.get(f"/api/v1/courses/{course_id}/publish-runs", headers=owner).json()["data"]
    assert [r["id"] for r in runs] == [run["id"]]
    one = client.get(f"/api/v1/courses/{course_id}/publish-runs/{run['id']}", headers=owner)
    assert one.json()["data"]["status"] == "pending"
    stranger = client.get(
        f"/api/v1/courses/{course_id}/publish-runs/{run['id']}", headers=tokens("instructor")
    )
    assert stranger.status_code == 403


async def _complete_publish(run_id: uuid.UUID) -> None:
    async with postgres.database.session_scope() as session:
        await publishing.complete_publish(session, run_id)


def test_unpublish_and_metadata_updates(client: TestClient, tokens: TokenFactory) -> None:
    owner_id = uuid.uuid4()
    owner = tokens("instructor", owner_id)
    course_id, _, lesson_id = _build_course(client, owner)
    run = client.post(f"/api/v1/courses/{course_id}/publish", headers=owner | _key()).json()["data"]
    # Stand in for the workflow's last step, on the app's own event loop.
    assert client.portal is not None
    client.portal.call(_complete_publish, uuid.UUID(run["id"]))

    student = tokens("student")
    assert client.get(f"/api/v1/lessons/{lesson_id}", headers=student).status_code == 200
    raised = client.patch(
        f"/api/v1/courses/{course_id}", headers=owner, json={"max_enrollments": 50}
    )
    assert raised.json()["data"]["max_enrollments"] == 50
    kept = client.patch(f"/api/v1/courses/{course_id}", headers=owner, json={"title": "DS 2"})
    assert kept.json()["data"]["max_enrollments"] == 50  # omitted → unchanged

    unpublished = client.post(f"/api/v1/courses/{course_id}/unpublish", headers=owner)
    assert unpublished.json()["data"]["status"] == "draft"
    assert client.get(f"/api/v1/lessons/{lesson_id}", headers=student).status_code == 404
