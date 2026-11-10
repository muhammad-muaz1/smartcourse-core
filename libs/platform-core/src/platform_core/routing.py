"""Auto-discovers every module's routes.py inside a service and mounts its router under
the API prefix. Adding a module means creating the folder — never editing a central
registration list.

A module folder without an ``__init__.py`` is skipped silently by pkgutil — it simply
never appears in /openapi.json (docs/adr/0009).
"""

import importlib
import pkgutil
from types import ModuleType

from fastapi import APIRouter


def discover_routers(modules_package: ModuleType) -> list[APIRouter]:
    routers: list[APIRouter] = []
    package_name = modules_package.__name__
    for module_info in sorted(pkgutil.iter_modules(modules_package.__path__), key=lambda m: m.name):
        try:
            routes_module = importlib.import_module(f"{package_name}.{module_info.name}.routes")
        except ModuleNotFoundError:
            continue
        router = getattr(routes_module, "router", None)
        if isinstance(router, APIRouter):
            routers.append(router)
    return routers


def build_api_router(modules_package: ModuleType, *, api_prefix: str) -> APIRouter:
    api_router = APIRouter(prefix=api_prefix)
    for module_router in discover_routers(modules_package):
        api_router.include_router(module_router)
    return api_router
