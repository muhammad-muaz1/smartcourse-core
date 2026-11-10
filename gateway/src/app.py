"""create_app() — assembles the gateway: routing to services, coarse token checks,
request-id propagation and CORS at the edge. No data, no business rules, ever.

The gateway has no OpenAPI docs of its own; each service serves its own at /docs on
its own port.
"""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response

from platform_core.exceptions import NotFoundError
from platform_core.handlers import register_exception_handlers
from platform_core.health import Check, build_health_router
from platform_core.logging import configure_logging
from platform_core.middleware import install_middleware
from src.auth import JWKSVerifier
from src.config import Settings, get_settings
from src.proxy import forward, resolve_upstream, route_table

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


def _upstream_check(client: httpx.AsyncClient, base_url: str) -> Check:
    async def check() -> None:
        response = await client.get(f"{base_url}/health")
        if response.status_code != 200:
            raise RuntimeError(f"/health returned {response.status_code}")

    return check


def _build_lifespan(
    client: httpx.AsyncClient,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await client.aclose()

    return lifespan


def create_app(
    settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    """``transport`` exists for tests, to stand in for the upstream services."""
    settings = settings or get_settings()
    upstreams = settings.upstreams

    configure_logging(
        service_name=settings.app.service_name,
        level=settings.log.level,
        json_logs=settings.log.json_logs,
    )
    # httpx logs every upstream call at INFO, duplicating the access line the request
    # already gets; its warnings and errors still come through.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    client = httpx.AsyncClient(timeout=upstreams.timeout_seconds, transport=transport)
    verifier = JWKSVerifier(
        client,
        jwks_url=f"{upstreams.identity_url}/.well-known/jwks.json",
        ttl_seconds=upstreams.jwks_cache_ttl_seconds,
    )
    routes = route_table(api_prefix=settings.app.api_prefix, upstreams=upstreams)

    app = FastAPI(
        title=settings.app.service_name,
        debug=settings.app.debug,
        lifespan=_build_lifespan(client),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    register_exception_handlers(app)
    install_middleware(app, cors=settings.cors)

    # The gateway's /health is healthy only when every service behind it is.
    checks = {
        "identity": _upstream_check(client, upstreams.identity_url),
        "course": _upstream_check(client, upstreams.course_url),
    }
    app.include_router(build_health_router(checks=checks, health=settings.health, app=settings.app))

    # Registered last so /health and /version above match first.
    @app.api_route("/{path:path}", methods=_METHODS, include_in_schema=False)
    async def proxy(request: Request, path: str) -> Response:
        base_url = resolve_upstream(request.url.path, routes)
        if base_url is None:
            raise NotFoundError(f"No service handles {request.url.path}")
        await verifier.reject_forged(request.headers.get("authorization"))
        return await forward(request, client, base_url)

    return app
