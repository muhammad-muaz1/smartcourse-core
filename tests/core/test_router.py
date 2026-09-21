import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.modules as modules_package
from src.core.router import _discover_module_routers, build_api_router


def test_health_module_is_discovered() -> None:
    routers = _discover_module_routers()
    assert any(any(route.path == "/health" for route in r.routes) for r in routers)  # type: ignore[attr-defined]


def test_build_api_router_mounts_health_under_the_api_prefix() -> None:
    app = FastAPI()
    app.include_router(build_api_router(api_prefix="/api/v1"))
    client = TestClient(app)

    response = client.get("/api/v1/version")

    assert response.status_code == 200


@pytest.fixture
def fake_module_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Builds src/modules/fake_widgets/routes.py under a temp dir, then points the real
    modules package's __path__ at it — the same mechanism a real module relies on, just
    pointed somewhere disposable for the test.
    """
    package_dir = tmp_path / "fake_widgets"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("")
    (package_dir / "routes.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/widgets')\n"
        "\n"
        "\n"
        "@router.get('/')\n"
        "def list_widgets() -> list[str]:\n"
        "    return []\n"
    )

    # Point discovery at both the temp dir *and* the real modules dir, so /health
    # keeps working for tests that hit it through the same app instance.
    monkeypatch.setattr(
        modules_package, "__path__", [str(package_dir.parent), *modules_package.__path__]
    )

    try:
        yield
    finally:
        for name in list(sys.modules):
            if name.startswith("src.modules.fake_widgets"):
                del sys.modules[name]


def test_discover_module_routers_finds_a_new_module_without_editing_anything(
    fake_module_package: None,
) -> None:
    routers = _discover_module_routers()
    assert any(r.prefix == "/widgets" for r in routers)


def test_build_api_router_mounts_the_new_module_under_the_api_prefix(
    fake_module_package: None,
) -> None:
    app = FastAPI()
    app.include_router(build_api_router(api_prefix="/api/v1"))
    client = TestClient(app)

    response = client.get("/api/v1/widgets/")

    assert response.status_code == 200
    assert response.json() == []
