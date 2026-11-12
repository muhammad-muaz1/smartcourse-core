"""``Idempotency-Key`` for the routes that need it (API design §13, ★ endpoints).

A client that retries a POST — a timeout, a flaky mobile connection, a double click —
must get the *original* response back, not a second enrollment or a second publish run.
Key + request fingerprint → stored response in Redis for 24h:

- same key, same request, first one finished  → the stored response, replayed verbatim
  (``Idempotent-Replayed: true``)
- same key, same request, first one running   → ``409 IDEMPOTENCY_REQUEST_IN_PROGRESS``
- same key, different request                  → ``409 IDEMPOTENCY_KEY_REUSED``
- no key on a route that requires one          → ``400 IDEMPOTENCY_KEY_REQUIRED``

Keys are namespaced by the *verified* token subject, so one user's key can never replay
another user's response. A request whose token doesn't verify is passed straight
through: the route rejects it with 401, and nothing is stored.

Middleware rather than a dependency because it must store the response the client
actually received — after the route's transaction has committed. 5xx responses are
never stored, so a retry after a server failure runs again.

Redis holds nothing durable (CLAUDE.md §6). If Redis is unreachable this fails *open*
and logs: the database invariants (unique enrollment, publish state machine) remain the
backstop, and a Redis blip must not take enrollment down with it.
"""

import base64
import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from platform_core.config import IdempotencySettings
from platform_core.handlers import error_body

logger = structlog.get_logger(__name__)

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
_MAX_KEY_LENGTH = 255
# Only these response headers are replayed; anything per-request (request id, date,
# CORS) is regenerated for the retry.
_REPLAYED_RESPONSE_HEADERS = frozenset({"content-type", "location"})


@dataclass(frozen=True, slots=True)
class IdempotentRoute:
    method: str
    path_pattern: str

    def matches(self, method: str, path: str) -> bool:
        return method == self.method and re.fullmatch(self.path_pattern, path) is not None


@dataclass(frozen=True, slots=True)
class IdempotencyOptions:
    routes: Sequence[IdempotentRoute]
    # Called per request, after startup, so the Redis client can be created in the
    # lifespan rather than when the middleware is constructed.
    redis: Callable[[], Redis]
    # Authorization header → verified subject, or None if the token doesn't verify.
    subject_resolver: Callable[[str | None], str | None]
    settings: IdempotencySettings
    namespace: str


class IdempotencyMiddleware:
    def __init__(self, app: ASGIApp, *, options: IdempotencyOptions) -> None:
        self.app = app
        self.options = options

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not any(
            route.matches(scope["method"], scope["path"]) for route in self.options.routes
        ):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        subject = self.options.subject_resolver(headers.get("authorization"))
        if subject is None:
            await self.app(scope, receive, send)
            return

        key = headers.get(IDEMPOTENCY_KEY_HEADER)
        if not key or len(key) > _MAX_KEY_LENGTH:
            await _error(
                scope,
                receive,
                send,
                status_code=400,
                code="IDEMPOTENCY_KEY_REQUIRED",
                message=f"This endpoint requires an {IDEMPOTENCY_KEY_HEADER} header "
                f"(1-{_MAX_KEY_LENGTH} characters)",
            )
            return

        body = await _read_body(receive)
        replay_receive = _replaying(body)
        fingerprint = _fingerprint(scope, body)
        redis_key = (
            f"idem:{self.options.namespace}:{subject}:{hashlib.sha256(key.encode()).hexdigest()}"
        )

        try:
            acquired = await self._acquire(redis_key, fingerprint)
            if not acquired:
                await self._answer_from_record(redis_key, fingerprint, scope, replay_receive, send)
                return
        except RedisError as exc:
            await logger.awarning("idempotency_unavailable", detail=str(exc))
            await self.app(scope, replay_receive, send)
            return

        await self._run_and_store(redis_key, fingerprint, scope, replay_receive, send)

    async def _acquire(self, redis_key: str, fingerprint: str) -> bool:
        marker = json.dumps({"state": "in_flight", "fingerprint": fingerprint})
        acquired = await self.options.redis().set(
            redis_key, marker, nx=True, ex=self.options.settings.in_flight_ttl_seconds
        )
        return bool(acquired)

    async def _answer_from_record(
        self, redis_key: str, fingerprint: str, scope: Scope, receive: Receive, send: Send
    ) -> None:
        raw = await self.options.redis().get(redis_key)
        if raw is None:
            # Expired between our SET NX and this GET — vanishingly rare; treat the
            # retry as still racing the original rather than running it twice.
            raw = json.dumps({"state": "in_flight", "fingerprint": fingerprint})
        record: dict[str, Any] = json.loads(raw)

        if record["fingerprint"] != fingerprint:
            await _error(
                scope,
                receive,
                send,
                status_code=409,
                code="IDEMPOTENCY_KEY_REUSED",
                message=f"This {IDEMPOTENCY_KEY_HEADER} was already used for a different request",
            )
        elif record["state"] == "in_flight":
            await _error(
                scope,
                receive,
                send,
                status_code=409,
                code="IDEMPOTENCY_REQUEST_IN_PROGRESS",
                message="A request with this Idempotency-Key is still being processed",
            )
        else:
            await _replay(record, send)

    async def _run_and_store(
        self, redis_key: str, fingerprint: str, scope: Scope, receive: Receive, send: Send
    ) -> None:
        status_code = 500
        response_headers: list[tuple[str, str]] = []
        chunks: list[bytes] = []

        async def capture(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_headers.extend(
                    (name.decode("latin-1"), value.decode("latin-1"))
                    for name, value in message.get("headers", [])
                    if name.decode("latin-1").lower() in _REPLAYED_RESPONSE_HEADERS
                )
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, capture)
        except BaseException:
            await self._release(redis_key)
            raise

        if status_code >= 500:
            await self._release(redis_key)
            return

        record = {
            "state": "done",
            "fingerprint": fingerprint,
            "status": status_code,
            "headers": response_headers,
            "body": base64.b64encode(b"".join(chunks)).decode("ascii"),
        }
        try:
            await self.options.redis().set(
                redis_key, json.dumps(record), ex=self.options.settings.response_ttl_seconds
            )
        except RedisError as exc:
            await logger.awarning("idempotency_store_failed", detail=str(exc))

    async def _release(self, redis_key: str) -> None:
        try:
            await self.options.redis().delete(redis_key)
        except RedisError as exc:
            await logger.awarning("idempotency_release_failed", detail=str(exc))


def _fingerprint(scope: Scope, body: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(scope["method"].encode())
    digest.update(b"\0")
    digest.update(scope["path"].encode())
    digest.update(b"\0")
    digest.update(scope.get("query_string", b""))
    digest.update(b"\0")
    digest.update(body)
    return digest.hexdigest()


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def _replaying(body: bytes) -> Receive:
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


async def _replay(record: dict[str, Any], send: Send) -> None:
    body = base64.b64decode(record["body"])
    headers = [
        (name.encode("latin-1"), value.encode("latin-1")) for name, value in record["headers"]
    ]
    headers.append((b"content-length", str(len(body)).encode()))
    headers.append((REPLAYED_HEADER.lower().encode(), b"true"))
    await send({"type": "http.response.start", "status": record["status"], "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _error(
    scope: Scope, receive: Receive, send: Send, *, status_code: int, code: str, message: str
) -> None:
    response = JSONResponse(status_code=status_code, content=error_body(code=code, message=message))
    await response(scope, receive, send)
