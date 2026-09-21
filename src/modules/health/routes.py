"""GET /health — overall status + per-dependency breakdown, 200 when healthy, 503
otherwise. GET /version — git SHA + build time, no dependency access, so it works even
when everything else is down.

No /health/live, /health/startup or /health/db. If a Kubernetes liveness probe needs to
avoid checking dependencies, point it at /version and revisit this then — see CLAUDE.md
§5's own note on this.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from src.config import get_settings
from src.modules.health.schemas import HealthResponse, VersionResponse
from src.modules.health.service import check_health

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> JSONResponse:
    healthy, services = await check_health()
    body = HealthResponse(
        status="ok" if healthy else "error",
        version=get_settings().app.git_sha,
        services=services,
    )
    return JSONResponse(
        status_code=200 if healthy else 503,
        content=body.model_dump(mode="json", exclude_none=True),
    )


@router.get("/version", response_model=VersionResponse)
async def version() -> VersionResponse:
    settings = get_settings()
    return VersionResponse(version=settings.app.git_sha, build_time=settings.app.build_time)
