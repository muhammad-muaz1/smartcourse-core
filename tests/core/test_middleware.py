from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.middleware import REQUEST_ID_HEADER, RequestContextMiddleware


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"status": "ok"}

    app.add_middleware(RequestContextMiddleware)
    return app


client = TestClient(_build_app())


def test_request_id_is_generated_when_absent() -> None:
    response = client.get("/ping")
    assert response.headers[REQUEST_ID_HEADER]


def test_request_id_is_echoed_back_when_provided() -> None:
    response = client.get("/ping", headers={REQUEST_ID_HEADER: "client-supplied-id"})
    assert response.headers[REQUEST_ID_HEADER] == "client-supplied-id"
