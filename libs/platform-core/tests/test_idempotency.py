"""IdempotencyMiddleware behaviour, end to end through a real FastAPI app. Redis is an
in-memory stand-in implementing the three commands the middleware uses, so these run
without Docker; the Redis client itself is covered by test_db_redis.py."""

import hashlib
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from platform_core.config import CorsSettings, IdempotencySettings
from platform_core.handlers import register_exception_handlers
from platform_core.idempotency import IdempotencyOptions, IdempotentRoute
from platform_core.middleware import install_middleware


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.down = False

    def _check(self) -> None:
        if self.down:
            raise RedisConnectionError("redis is down")

    async def get(self, key: str) -> str | None:
        self._check()
        return self.data.get(key)

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        self._check()
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    async def delete(self, key: str) -> int:
        self._check()
        return 1 if self.data.pop(key, None) is not None else 0


class App:
    def __init__(self) -> None:
        self.redis = FakeRedis()
        self.calls = 0
        self.fail_next = False
        app = FastAPI()
        register_exception_handlers(app)

        @app.post("/things/{thing_id}/enrol", status_code=201)
        async def enrol(thing_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
            self.calls += 1
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("boom")
            return {"call": self.calls, "thing": thing_id, "payload": payload}

        @app.post("/other")
        async def other() -> dict[str, int]:
            self.calls += 1
            return {"call": self.calls}

        install_middleware(
            app,
            cors=CorsSettings(),
            idempotency=IdempotencyOptions(
                routes=[IdempotentRoute("POST", r"/things/[^/]+/enrol")],
                redis=lambda: self.redis,  # type: ignore[arg-type,return-value]  # duck-typed stand-in
                subject_resolver=lambda header: header.removeprefix("Bearer ") if header else None,
                settings=IdempotencySettings(),
                namespace="test",
            ),
        )
        self.client = TestClient(app, raise_server_exceptions=False)

    def enrol(self, *, user: str | None = "alice", key: str | None = "k1", body: Any = None) -> Any:
        headers = {}
        if user:
            headers["Authorization"] = f"Bearer {user}"
        if key:
            headers["Idempotency-Key"] = key
        return self.client.post("/things/t1/enrol", headers=headers, json=body)


@pytest.fixture
def app() -> App:
    return App()


def test_a_retry_with_the_same_key_replays_the_first_response(app: App) -> None:
    first = app.enrol(body={"a": 1})
    second = app.enrol(body={"a": 1})

    assert first.status_code == second.status_code == 201
    assert second.json() == first.json()
    assert second.headers["idempotent-replayed"] == "true"
    assert "idempotent-replayed" not in first.headers
    assert app.calls == 1


def test_missing_key_is_400_and_the_route_never_runs(app: App) -> None:
    response = app.enrol(key=None)

    assert response.status_code == 400
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert "request_id" in response.json()
    assert app.calls == 0


def test_same_key_with_a_different_body_is_409(app: App) -> None:
    app.enrol(body={"a": 1})
    response = app.enrol(body={"a": 2})

    assert response.status_code == 409
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert app.calls == 1


def test_a_request_still_in_flight_is_409(app: App) -> None:
    app.redis.data["idem:test:alice:" + _sha("k1")] = (
        '{"state": "in_flight", "fingerprint": "' + _fingerprint_of(app) + '"}'
    )
    response = app.enrol()

    assert response.status_code == 409
    assert response.json()["code"] == "IDEMPOTENCY_REQUEST_IN_PROGRESS"


def test_keys_are_scoped_per_user(app: App) -> None:
    alice = app.enrol(user="alice")
    bob = app.enrol(user="bob")

    assert alice.json()["call"] == 1
    assert bob.json()["call"] == 2
    assert "idempotent-replayed" not in bob.headers


def test_a_server_error_is_not_stored_so_a_retry_runs_again(app: App) -> None:
    app.fail_next = True
    failed = app.enrol()
    retried = app.enrol()

    assert failed.status_code == 500
    assert retried.status_code == 201
    assert app.calls == 2


def test_an_unauthenticated_request_passes_through_to_the_route(app: App) -> None:
    response = app.enrol(user=None, key=None)

    assert response.status_code == 201  # the stand-in route has no auth; a real one 401s
    assert app.redis.data == {}


def test_routes_not_listed_are_untouched(app: App) -> None:
    response = app.client.post("/other", headers={"Authorization": "Bearer alice"})

    assert response.status_code == 200
    assert app.redis.data == {}


def test_redis_down_fails_open(app: App) -> None:
    app.redis.down = True
    first = app.enrol()
    second = app.enrol()

    assert first.status_code == second.status_code == 201
    assert app.calls == 2


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _fingerprint_of(_app: App) -> str:
    """The fingerprint the middleware computes for `app.enrol()` with no body."""
    digest = hashlib.sha256()
    for part in (b"POST", b"/things/t1/enrol", b""):
        digest.update(part)
        digest.update(b"\0")
    return digest.hexdigest()
