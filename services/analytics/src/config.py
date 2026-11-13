"""All analytics settings, nested by concern. Every environment variable is prefixed
``SMARTCOURSE_`` and nested groups are joined with ``__``.

Read from this service's own ``.env`` (``services/analytics/.env``) — never another
service's.
"""

from functools import lru_cache

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from platform_core.config import (
    AppSettings,
    CorsSettings,
    HealthSettings,
    JWTVerifySettings,
    KafkaSettings,
    LogSettings,
    MetricsSettings,
    PostgresSettings,
    RedisSettings,
    TelemetrySettings,
)


class AnalyticsAppSettings(AppSettings):
    service_name: str = "analytics"
    port: int = 8005


class AnalyticsPostgresSettings(PostgresSettings):
    dsn: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/analytics_db"


class AnalyticsRedisSettings(RedisSettings):
    # DB 5 is analytics'. Metric responses, cached for the TTLs below.
    url: str = "redis://localhost:6379/5"


class AnalyticsMetricsSettings(MetricsSettings):
    worker_port: int = 9105


class CacheSettings(BaseModel):
    """Response TTLs (API design §10). Short on purpose: the projections are already
    seconds behind the source; the cache only absorbs dashboard refresh storms."""

    overview_seconds: int = 60
    timeseries_seconds: int = 300
    course_load_seconds: int = 900
    failures_seconds: int = 60


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SMARTCOURSE_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app: AnalyticsAppSettings = Field(default_factory=AnalyticsAppSettings)
    log: LogSettings = Field(default_factory=LogSettings)
    cors: CorsSettings = Field(default_factory=CorsSettings)
    postgres: AnalyticsPostgresSettings = Field(default_factory=AnalyticsPostgresSettings)
    redis: AnalyticsRedisSettings = Field(default_factory=AnalyticsRedisSettings)
    health: HealthSettings = Field(default_factory=HealthSettings)
    # Public key only — this service verifies tokens, it never issues them.
    security: JWTVerifySettings = Field(default_factory=JWTVerifySettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    # Worker process only — the API never talks to Kafka.
    kafka: KafkaSettings = Field(default_factory=KafkaSettings)
    metrics: AnalyticsMetricsSettings = Field(default_factory=AnalyticsMetricsSettings)


@lru_cache
def get_settings() -> Settings:
    return Settings()
