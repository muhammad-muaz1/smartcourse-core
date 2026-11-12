"""Gateway settings. Same ``SMARTCOURSE_`` prefix and ``__`` nesting as every service,
read from ``gateway/.env``.

The gateway holds no data and no business rules — only where each service lives and
how long to wait for it.
"""

from functools import lru_cache

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from platform_core.config import AppSettings, CorsSettings, HealthSettings, LogSettings


class GatewayAppSettings(AppSettings):
    service_name: str = "gateway"
    port: int = 8000


class UpstreamSettings(BaseModel):
    identity_url: str = "http://localhost:8001"
    course_url: str = "http://localhost:8002"
    learning_url: str = "http://localhost:8003"
    timeout_seconds: float = 10.0
    # How long identity's public keys are trusted before being re-fetched. A token
    # with an unknown kid forces an early re-fetch, so a key rotation isn't blocked by
    # this TTL.
    jwks_cache_ttl_seconds: float = 300.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SMARTCOURSE_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app: GatewayAppSettings = Field(default_factory=GatewayAppSettings)
    log: LogSettings = Field(default_factory=LogSettings)
    cors: CorsSettings = Field(default_factory=CorsSettings)
    health: HealthSettings = Field(default_factory=HealthSettings)
    upstreams: UpstreamSettings = Field(default_factory=UpstreamSettings)


@lru_cache
def get_settings() -> Settings:
    return Settings()
