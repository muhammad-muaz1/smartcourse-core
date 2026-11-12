import importlib
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from platform_core.routing import build_api_router, discover_attribute, discover_routers


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


def test_discover_attribute_collects_one_value_per_module_that_defines_it(
    modules_package: ModuleType, tmp_path: Path
) -> None:
    root = tmp_path / "fake_service_modules"
    (root / "widgets" / "activities.py").write_text("ACTIVITIES = ['a', 'b']\n")
    (root / "no_routes" / "activities.py").write_text("UNRELATED = 1\n")

    assert discover_attribute(modules_package, "activities", "ACTIVITIES") == [["a", "b"]]


def test_discover_attribute_surfaces_a_broken_import_inside_the_file(
    modules_package: ModuleType, tmp_path: Path
) -> None:
    root = tmp_path / "fake_service_modules"
    (root / "widgets" / "workflows.py").write_text("import module_that_does_not_exist\n")

    with pytest.raises(ModuleNotFoundError):
        discover_attribute(modules_package, "workflows", "WORKFLOWS")
