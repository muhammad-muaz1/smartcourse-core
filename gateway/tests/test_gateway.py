"""The gateway against fake upstreams (httpx.MockTransport) — no services, no network."""

import base64
import time
import uuid
from collections.abc import Callable, Iterator

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from src.app import create_app
from src.config import Settings, UpstreamSettings

IDENTITY = "http://identity.test"
COURSE = "http://course.test"
LEARNING = "http://learning.test"


def _b64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class FakeUpstreams:
    """Records every forwarded request; serves JWKS for one real RSA key."""

    def __init__(self) -> None:
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.requests: list[httpx.Request] = []
        self.handler: Callable[[httpx.Request], httpx.Response] | None = None

    def jwks(self) -> dict[str, object]:
        numbers = self.private_key.public_key().public_numbers()
        key = {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": "dev-1"}
        key |= {"n": _b64url_uint(numbers.n), "e": _b64url_uint(numbers.e)}
        return {"keys": [key]}

    def token(self, *, signing_key: rsa.RSAPrivateKey | None = None, ttl: int = 900) -> str:
        pem = (signing_key or self.private_key).private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        now = int(time.time())
        claims = {"sub": str(uuid.uuid4()), "role": "student", "type": "access"}
        claims |= {"jti": str(uuid.uuid4()), "iat": now, "exp": now + ttl}
        return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": "dev-1"})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/jwks.json":
            return httpx.Response(200, json=self.jwks())
        self.requests.append(request)
        if self.handler is not None:
            return self.handler(request)
        return httpx.Response(200, json={"host": request.url.host, "path": request.url.path})


@pytest.fixture
def upstreams() -> FakeUpstreams:
    return FakeUpstreams()


@pytest.fixture
def client(upstreams: FakeUpstreams) -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,
        upstreams=UpstreamSettings(identity_url=IDENTITY, course_url=COURSE, learning_url=LEARNING),
    )
    app = create_app(settings, transport=httpx.MockTransport(upstreams))
    with TestClient(app) as test_client:
        yield test_client


def test_auth_routes_go_to_identity(client: TestClient, upstreams: FakeUpstreams) -> None:
    response = client.post("/api/v1/auth/login", json={"email": "a@example.com"})

    assert response.status_code == 200
    assert response.json() == {"host": "identity.test", "path": "/api/v1/auth/login"}
    assert upstreams.requests[0].content == b'{"email":"a@example.com"}'


def test_course_routes_go_to_course_with_the_query_string(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    response = client.get("/api/v1/courses?q=python&limit=5")

    assert response.json()["host"] == "course.test"
    assert upstreams.requests[0].url.query == b"q=python&limit=5"


@pytest.mark.parametrize(
    ("path", "host"),
    [
        ("/api/v1/courses/3f6c1c1e-0000-4000-8000-000000000000/enrollments", "learning.test"),
        ("/api/v1/users/3f6c1c1e-0000-4000-8000-000000000000/enrollment-history", "learning.test"),
        ("/api/v1/enrollments/me", "learning.test"),
        ("/api/v1/courses/3f6c1c1e-0000-4000-8000-000000000000/modules", "course.test"),
        ("/api/v1/courses/3f6c1c1e-0000-4000-8000-000000000000/publish", "course.test"),
        ("/api/v1/modules/3f6c1c1e-0000-4000-8000-000000000000/lessons", "course.test"),
        ("/api/v1/lessons/3f6c1c1e-0000-4000-8000-000000000000", "course.test"),
        ("/api/v1/users/3f6c1c1e-0000-4000-8000-000000000000", "identity.test"),
    ],
)
def test_nested_resources_go_to_the_service_that_owns_them(
    client: TestClient, path: str, host: str
) -> None:
    assert client.get(path).json()["host"] == host


def test_jwks_is_served_from_identity_at_the_host_root(client: TestClient) -> None:
    response = client.get("/.well-known/jwks.json")

    assert response.json()["keys"][0]["kid"] == "dev-1"


def test_a_prefix_only_matches_whole_path_segments(client: TestClient) -> None:
    response = client.get("/api/v1/courses-archive")

    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_request_id_and_client_ip_are_forwarded(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    response = client.get("/api/v1/courses", headers={"X-Request-ID": "req-123"})

    forwarded = upstreams.requests[0].headers
    assert forwarded["x-request-id"] == "req-123"
    assert forwarded["x-forwarded-for"] == "testclient"
    assert response.headers["x-request-id"] == "req-123"


def test_upstream_status_and_headers_pass_through(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    upstreams.handler = lambda _request: httpx.Response(
        409, json={"code": "CONFLICT"}, headers={"x-custom": "1"}
    )

    response = client.post("/api/v1/auth/register", json={})

    assert response.status_code == 409
    assert response.json() == {"code": "CONFLICT"}
    assert response.headers["x-custom"] == "1"


def test_an_unreachable_service_is_a_502_in_the_standard_error_shape(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    upstreams.handler = refuse

    response = client.get("/api/v1/courses")

    assert response.status_code == 502
    assert response.json()["code"] == "UPSTREAM_UNAVAILABLE"
    assert response.json()["request_id"]


def test_a_token_signed_by_another_key_is_rejected_at_the_edge(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    forged = upstreams.token(signing_key=rsa.generate_private_key(65537, 2048))

    response = client.get("/api/v1/courses", headers={"Authorization": f"Bearer {forged}"})

    assert response.status_code == 401
    assert upstreams.requests == []


def test_garbage_bearer_token_is_rejected_at_the_edge(client: TestClient) -> None:
    response = client.get("/api/v1/courses", headers={"Authorization": "Bearer not-a-jwt"})

    assert response.status_code == 401


def test_a_validly_signed_token_is_forwarded(client: TestClient, upstreams: FakeUpstreams) -> None:
    token = upstreams.token()

    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert upstreams.requests[0].headers["authorization"] == f"Bearer {token}"


def test_an_expired_but_genuine_token_still_reaches_the_service(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    """Clients send their expired access token alongside /auth/refresh; expiry is the
    service's call, not the gateway's."""
    expired = upstreams.token(ttl=-60)

    response = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": "x"},
        headers={"Authorization": f"Bearer {expired}"},
    )

    assert response.status_code == 200


def test_health_is_ok_only_when_every_service_is(
    client: TestClient, upstreams: FakeUpstreams
) -> None:
    healthy = client.get("/health")
    assert healthy.status_code == 200
    assert set(healthy.json()["services"]) == {"identity", "course", "learning"}


def test_health_is_503_when_a_service_is_down(upstreams: FakeUpstreams) -> None:
    upstreams.handler = lambda request: httpx.Response(
        503 if request.url.host == "course.test" else 200
    )
    settings = Settings(
        _env_file=None,
        upstreams=UpstreamSettings(identity_url=IDENTITY, course_url=COURSE, learning_url=LEARNING),
    )
    with TestClient(create_app(settings, transport=httpx.MockTransport(upstreams))) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["services"]["course"]["status"] == "error"
    assert response.json()["services"]["identity"]["status"] == "ok"
