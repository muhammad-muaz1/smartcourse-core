"""Auto-discovers every module's routes.py and mounts its router under /api/v1.
Adding a module means creating the folder — never editing a central registration list.
"""

import importlib
import pkgutil

from fastapi import APIRouter

import src.modules as modules_package


def _discover_module_routers() -> list[APIRouter]:
    routers: list[APIRouter] = []
    for module_info in sorted(pkgutil.iter_modules(modules_package.__path__), key=lambda m: m.name):
        try:
            routes_module = importlib.import_module(f"src.modules.{module_info.name}.routes")
        except ModuleNotFoundError:
            continue
        router = getattr(routes_module, "router", None)
        if isinstance(router, APIRouter):
            routers.append(router)
    return routers


def build_api_router(*, api_prefix: str) -> APIRouter:
    api_router = APIRouter(prefix=api_prefix)
    for module_router in _discover_module_routers():
        api_router.include_router(module_router)
    return api_router
