"""Auto-discovers every module's routes.py inside a service and mounts its router under
the API prefix. Adding a module means creating the folder — never editing a central
registration list.

A module folder without an ``__init__.py`` is skipped silently by pkgutil — it simply
never appears in /openapi.json (docs/adr/0009).
"""

import importlib
import pkgutil
from types import ModuleType
from typing import Any

from fastapi import APIRouter


def discover_attribute(modules_package: ModuleType, submodule: str, attribute: str) -> list[Any]:
    """``attribute`` from every ``<modules_package>.<module>.<submodule>`` that defines
    it, in module-name order. The same auto-discovery routes use, applied to Temporal
    ``workflows``/``activities`` (and later ``consumers``/``tasks``): adding one means
    adding the file, never editing a central list."""
    found: list[Any] = []
    package_name = modules_package.__name__
    for module_info in sorted(pkgutil.iter_modules(modules_package.__path__), key=lambda m: m.name):
        qualified = f"{package_name}.{module_info.name}.{submodule}"
        try:
            module = importlib.import_module(qualified)
        except ModuleNotFoundError as exc:
            # Only "this module has no such file" is skippable. A missing import
            # *inside* the file is a real bug and must surface.
            if exc.name != qualified:
                raise
            continue
        value = getattr(module, attribute, None)
        if value is not None:
            found.append(value)
    return found


def discover_routers(modules_package: ModuleType) -> list[APIRouter]:
    return [
        router
        for router in discover_attribute(modules_package, "routes", "router")
        if isinstance(router, APIRouter)
    ]


def build_api_router(modules_package: ModuleType, *, api_prefix: str) -> APIRouter:
    api_router = APIRouter(prefix=api_prefix)
    for module_router in discover_routers(modules_package):
        api_router.include_router(module_router)
    return api_router
