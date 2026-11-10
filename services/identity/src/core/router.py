"""Mounts every module's routes.py under the API prefix — see platform_core.routing."""

from fastapi import APIRouter

import src.modules as modules_package
from platform_core.routing import build_api_router as _build_api_router


def build_api_router(*, api_prefix: str) -> APIRouter:
    return _build_api_router(modules_package, api_prefix=api_prefix)
