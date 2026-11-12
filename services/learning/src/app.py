"""create_app() — assembles the learning service. main.py stays thin."""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI

from platform_core.handlers import register_exception_handlers
from platform_core.health import Check, build_health_router, log_unavailable_dependencies
from platform_core.idempotency import IdempotencyOptions, IdempotentRoute
from platform_core.logging import configure_logging
from platform_core.middleware import install_middleware
from platform_core.security import subject_from_authorization
from src.config import Settings, get_settings
from src.core.router import build_api_router
from src.db import postgres, redis

_CHECKS: dict[str, Check] = {"postgres": postgres.check, "redis": redis.check}

_UUID = r"[0-9a-fA-F-]{36}"


def _idempotency(settings: Settings) -> IdempotencyOptions:
    """The ★ endpoints this service owns (API design §13)."""
    prefix = settings.app.api_prefix
    return IdempotencyOptions(
        routes=[IdempotentRoute("POST", rf"{prefix}/courses/{_UUID}/enrollments")],
        redis=redis.get_redis,
        subject_resolver=lambda header: subject_from_authorization(
            header, settings=settings.security
        ),
        settings=settings.idempotency,
        namespace=settings.app.service_name,
    )


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

    app = FastAPI(
        title=settings.app.service_name,
        debug=settings.app.debug,
        lifespan=_build_lifespan(settings),
    )

    register_exception_handlers(app)
    install_middleware(app, cors=settings.cors, idempotency=_idempotency(settings))

    app.include_router(
        build_health_router(checks=_CHECKS, health=settings.health, app=settings.app)
    )
    app.include_router(build_api_router(api_prefix=settings.app.api_prefix))

    return app
