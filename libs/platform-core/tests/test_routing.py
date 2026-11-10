import importlib
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from platform_core.routing import build_api_router, discover_routers


@pytest.fixture
def modules_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    """Builds a throwaway ``fake_service_modules`` package with one module that has a
    routes.py, one without, and a folder missing __init__.py — the same shapes a real
    service's src/modules/ can contain."""
    root = tmp_path / "fake_service_modules"
    widgets = root / "widgets"
    widgets.mkdir(parents=True)
    (root / "__init__.py").write_text("")
    (widgets / "__init__.py").write_text("")
    (widgets / "routes.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/widgets')\n"
        "\n"
        "\n"
        "@router.get('/')\n"
        "def list_widgets() -> list[str]:\n"
        "    return []\n"
    )
    no_routes = root / "no_routes"
    no_routes.mkdir()
    (no_routes / "__init__.py").write_text("")
    (root / "not_a_package").mkdir()

    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        yield importlib.import_module("fake_service_modules")
    finally:
        for name in list(sys.modules):
            if name.startswith("fake_service_modules"):
                del sys.modules[name]


def test_discover_routers_finds_every_module_with_a_router(modules_package: ModuleType) -> None:
    routers = discover_routers(modules_package)

    assert [r.prefix for r in routers] == ["/widgets"]


def test_build_api_router_mounts_modules_under_the_api_prefix(
    modules_package: ModuleType,
) -> None:
    app = FastAPI()
    app.include_router(build_api_router(modules_package, api_prefix="/api/v1"))

    response = TestClient(app).get("/api/v1/widgets/")

    assert response.status_code == 200
    assert response.json() == []
