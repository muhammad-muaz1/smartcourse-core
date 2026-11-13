"""create_app() — assembles the analytics service. main.py stays thin."""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI

from platform_core.handlers import register_exception_handlers
from platform_core.health import Check, build_health_router, log_unavailable_dependencies
from platform_core.logging import configure_logging
from platform_core.middleware import install_middleware
from platform_core.telemetry import configure_tracing
from src.config import Settings, get_settings
from src.core.router import build_api_router
from src.db import postgres, redis

_CHECKS: dict[str, Check] = {"postgres": postgres.check, "redis": redis.check}


def _build_lifespan(
    settings: Settings,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        postgres.init_postgres(settings.postgres)
        redis.init_redis(settings.redis)
        await log_unavailable_dependencies(_CHECKS)
        try:
            yield
        finally:
            await redis.close_redis()
            await postgres.close_postgres()

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    configure_logging(
        service_name=settings.app.service_name,
        level=settings.log.level,
        json_logs=settings.log.json_logs,
    )
    configure_tracing(service_name=settings.app.service_name, settings=settings.telemetry)

    app = FastAPI(
        title=settings.app.service_name,
        debug=settings.app.debug,
        lifespan=_build_lifespan(settings),
    )

    register_exception_handlers(app)
    install_middleware(app, cors=settings.cors)

    app.include_router(
        build_health_router(checks=_CHECKS, health=settings.health, app=settings.app)
    )
    app.include_router(build_api_router(api_prefix=settings.app.api_prefix))

    return app
