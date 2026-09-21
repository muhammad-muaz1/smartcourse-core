from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from src.core.exceptions import BusinessRuleError, ConflictError, NotFoundError
from src.core.handlers import register_exception_handlers


class _Body(BaseModel):
    name: str


def _build_test_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/not-found")
    def not_found() -> None:
        raise NotFoundError("course 123 does not exist")

    @app.get("/conflict")
    def conflict() -> None:
        raise ConflictError("already enrolled")

    @app.get("/business-rule")
    def business_rule() -> None:
        raise BusinessRuleError("seat limit reached")

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("unexpected")

    @app.post("/validate")
    def validate(body: _Body) -> dict[str, str]:
        return {"name": body.name}

    return app


client = TestClient(_build_test_app(), raise_server_exceptions=False)


def test_not_found_error_maps_to_404_with_the_new_error_shape() -> None:
    response = client.get("/not-found")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "NOT_FOUND"
    assert body["message"] == "course 123 does not exist"
    assert body["details"] == []
    assert "request_id" in body


def test_conflict_error_maps_to_409() -> None:
    assert client.get("/conflict").status_code == 409


def test_business_rule_error_maps_to_422() -> None:
    assert client.get("/business-rule").status_code == 422


def test_unhandled_exception_becomes_500_without_leaking_internals() -> None:
    response = client.get("/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "INTERNAL_ERROR"
    assert "RuntimeError" not in body["message"]


def test_request_validation_error_reports_field_details() -> None:
    response = client.post("/validate", json={})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert body["details"]
    assert body["details"][0]["field"]
