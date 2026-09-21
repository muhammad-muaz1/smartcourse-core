from pydantic import BaseModel


class ServiceStatus(BaseModel):
    status: str
    latency_ms: float | None = None
    detail: str | None = None


class HealthResponse(BaseModel):
    status: str
    version: str
    services: dict[str, ServiceStatus]


class VersionResponse(BaseModel):
    version: str
    build_time: str
