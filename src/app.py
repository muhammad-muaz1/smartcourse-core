"""create_app() — assembles the whole application. main.py stays thin.

Middleware is added in the *reverse* of the execution order CLAUDE.md §6 lists.
Starlette's middleware stack is LIFO: the middleware added last wraps everything added
before it and therefore runs first on the way in (verified empirically — see
docs/adr/0003). To make RequestContextMiddleware execute first, it must be the *last*
``add_middleware`` call.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import structlog
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from src.config import Settings, get_settings
from src.core.handlers import register_exception_handlers
from src.core.logging import configure_logging
from src.core.middleware import RequestContextMiddleware
from src.core.router import build_api_router
from src.core.security import build_jwks
from src.db import mongo, postgres
from src.db import redis as redis_db

logger = structlog.get_logger(__name__)


def _build_lifespan(
    settings: Settings,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await _run_lifespan(settings)
        try:
            yield
        finally:
            await postgres.close_postgres()
            await mongo.close_mongo()
            await redis_db.close_redis()

    return lifespan


async def _run_lifespan(settings: Settings) -> None:
    postgres.init_postgres(settings.postgres)
    redis_db.init_redis(settings.redis)
    try:
        await mongo.init_mongo(settings.mongo)
    except Exception as exc:  # noqa: BLE001 - a Mongo outage must not crash the app
        await logger.awarning("mongo_init_failed_at_startup", detail=str(exc))

    # "Connections are established and verified at startup": ping each dependency now
    # so a problem is visible in logs immediately, without blocking the app from
    # starting — a dependency outage shows up in /health, it does not crash-loop the
    # process (that would make the outage worse).
    checks = (
        ("postgres", postgres.check),
        ("mongo", mongo.check),
        ("redis", redis_db.check),
    )
    for name, check in checks:
        try:
            await check()
        except Exception as exc:  # noqa: BLE001 - startup verification is best-effort
            await logger.awarning("dependency_unavailable_at_startup", name=name, detail=str(exc))


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    configure_logging(level=settings.log.level, json_logs=settings.log.json_logs)

    app = FastAPI(
        title=settings.app.service_name,
        debug=settings.app.debug,
        lifespan=_build_lifespan(settings),
    )

    register_exception_handlers(app)

    # Added in reverse of CLAUDE.md §6's execution order — see module docstring.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors.allow_origins,
        allow_credentials=settings.cors.allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestContextMiddleware)

    app.include_router(build_api_router(api_prefix=settings.app.api_prefix))

    # RFC 5785 requires this at the host root, not under /api/v1 — the one deliberate
    # exception to "every module router mounts under the API prefix" (CLAUDE.md §4).
    # Small enough that it doesn't need its own module.
    @app.get("/.well-known/jwks.json", tags=["auth"])
    async def jwks() -> dict[str, object]:
        return build_jwks(settings.security)

    return app
