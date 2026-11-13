"""``GET /health``, ``GET /version`` and ``GET /metrics`` for every service, plus the concurrent
per-dependency checks behind them.

/health: overall status + per-dependency breakdown, 200 when healthy, 503 otherwise.
Checks run concurrently, each with a timeout, results cached briefly so a probe hitting
/health every second doesn't hammer the dependencies themselves. Adding Kafka or
Temporal later is one more entry in the service's ``checks`` mapping.

/version: git SHA + build time, no dependency access, so it works even when everything
else is down — point a Kubernetes liveness probe here, not at /health (CLAUDE.md §11).

Both sit at the host root, outside /api/v1: they are infrastructure contracts and must
not move when the API version does.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping

import structlog
from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from platform_core.config import AppSettings, HealthSettings
from platform_core.metrics import metrics_response

logger = structlog.get_logger(__name__)

Check = Callable[[], Awaitable[None]]


class ServiceStatus(BaseModel):
    status: str
    latency_ms: float | None = None
    detail: str | None = None


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    services: dict[str, ServiceStatus]


class VersionResponse(BaseModel):
    service: str
    version: str
    build_time: str


class HealthChecker:
    def __init__(self, checks: Mapping[str, Check], settings: HealthSettings) -> None:
        self._checks = dict(checks)
        self._settings = settings
        self._cache: dict[str, tuple[ServiceStatus, float]] = {}

    async def _check_one(self, name: str, check_fn: Check) -> ServiceStatus:
        now = time.monotonic()
        cached = self._cache.get(name)
        if cached is not None and (now - cached[1]) < self._settings.cache_ttl_seconds:
            return cached[0]

        start = time.perf_counter()
        try:
            async with asyncio.timeout(self._settings.check_timeout_seconds):
                await check_fn()
        except TimeoutError:
            result = ServiceStatus(status="error", detail="timed out")
        except Exception as exc:  # noqa: BLE001 - any dependency failure means "error", not a crash
            result = ServiceStatus(status="error", detail=str(exc))
        else:
            latency_ms = round((time.perf_counter() - start) * 1000, 2)
            result = ServiceStatus(status="ok", latency_ms=latency_ms)

        self._cache[name] = (result, now)
        return result

    async def run(self) -> tuple[bool, dict[str, ServiceStatus]]:
        names = list(self._checks)
        results = await asyncio.gather(*(self._check_one(n, self._checks[n]) for n in names))
        services = dict(zip(names, results, strict=True))
        healthy = all(service.status == "ok" for service in services.values())
        return healthy, services


async def log_unavailable_dependencies(checks: Mapping[str, Check]) -> None:
    """Pings each dependency at startup so a problem is visible in logs immediately,
    without blocking the app from starting — a dependency outage shows up in /health,
    it does not crash-loop the process (that would make the outage worse)."""
    for name, check in checks.items():
        try:
            await check()
        except Exception as exc:  # noqa: BLE001 - startup verification is best-effort
            await logger.awarning("dependency_unavailable_at_startup", name=name, detail=str(exc))


def build_health_router(
    *, checks: Mapping[str, Check], health: HealthSettings, app: AppSettings
) -> APIRouter:
    checker = HealthChecker(checks, health)
    router = APIRouter(tags=["health"])

    @router.get("/health", response_model=HealthResponse)
    async def health_route() -> JSONResponse:
        healthy, services = await checker.run()
        body = HealthResponse(
            status="ok" if healthy else "error",
            service=app.service_name,
            version=app.git_sha,
            services=services,
        )
        return JSONResponse(
            status_code=200 if healthy else 503,
            content=body.model_dump(mode="json", exclude_none=True),
        )

    @router.get("/version", response_model=VersionResponse)
    async def version_route() -> VersionResponse:
        return VersionResponse(
            service=app.service_name, version=app.git_sha, build_time=app.build_time
        )

    @router.get("/metrics", include_in_schema=False)
    async def metrics_route() -> Response:
        return metrics_response()

    return router
