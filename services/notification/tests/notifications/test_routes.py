"""The inbox over HTTP with real token verification, Postgres and Redis. Requires Docker
(testcontainers) or SMARTCOURSE_TEST_POSTGRES_URL / SMARTCOURSE_TEST_REDIS_URL."""

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from platform_core.config import JWTVerifySettings
from src.app import create_app
from src.config import NotificationPostgresSettings, NotificationRedisSettings, Settings
from src.db import postgres
from src.modules.notifications import service
from tests.conftest import TokenFactory
from tests.notifications.helpers import MAX_AGE, user_event


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
        postgres=NotificationPostgresSettings(dsn=postgres_container.get_connection_url()),
        redis=NotificationRedisSettings(url=f"redis://{host}:{port}/4"),
        security=JWTVerifySettings(jwt_public_key_path=str(tokens.public_key_path)),
    )
    monkeypatch.setattr("src.core.dependencies.get_settings", lambda: settings)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _seed(client: TestClient, user_id: uuid.UUID) -> None:
    async def create() -> None:
        event = user_event(user_id)
        async with postgres.database.session_scope() as session:
            await service.upsert_recipient(session, event)
            await service.notify_account_created(session, event, max_age=MAX_AGE)

    assert client.portal is not None
    client.portal.call(create)


def test_a_user_reads_only_their_own_inbox(client: TestClient, tokens: TokenFactory) -> None:
    owner_id = uuid.uuid4()
    _seed(client, owner_id)
    owner, stranger = tokens("student", owner_id), tokens("student")

    inbox = client.get("/api/v1/notifications/me?unread=true", headers=owner).json()["data"]
    [item] = inbox["items"]
    assert (item["kind"], item["status"]) == ("account_welcome", "pending")
    assert client.get("/api/v1/notifications/me", headers=stranger).json()["data"]["items"] == []

    path = f"/api/v1/notifications/{item['id']}/read"
    assert client.post(path, headers=stranger).status_code == 404
    assert client.post(path, headers=owner).json()["data"]["read_at"] is not None
    assert client.post("/api/v1/notifications/read-all", headers=owner).json()["data"] == {
        "marked_read": 0
    }


def test_there_is_no_way_to_send_a_notification(client: TestClient, tokens: TokenFactory) -> None:
    """API design §9: records are written only by event consumers."""
    response = client.post("/api/v1/notifications", headers=tokens("admin"), json={})
    assert response.status_code in (404, 405)


def test_the_inbox_needs_a_token(client: TestClient) -> None:
    assert client.get("/api/v1/notifications/me").status_code == 401
