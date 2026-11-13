"""All learning settings, nested by concern. Every environment variable is prefixed
``SMARTCOURSE_`` and nested groups are joined with ``__``, e.g.
``SMARTCOURSE_POSTGRES__DSN=...`` sets ``settings.postgres.dsn``.

Read from this service's own ``.env`` (``services/learning/.env``) — never another
service's.

``get_settings()`` is called eagerly at app startup (not lazily on first request) so a
misconfigured deployment fails before it accepts traffic.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from platform_core.config import (
    AppSettings,
    CorsSettings,
    HealthSettings,
    IdempotencySettings,
    JWTVerifySettings,
    KafkaSettings,
    LogSettings,
    MetricsSettings,
    OutboxSettings,
    PostgresSettings,
    RedisSettings,
    TelemetrySettings,
)


class LearningAppSettings(AppSettings):
    service_name: str = "learning"
    port: int = 8003


class LearningPostgresSettings(PostgresSettings):
    dsn: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/learning_db"


class LearningRedisSettings(RedisSettings):
    # DB 3 is learning's. Today: Idempotency-Key responses for enrollment and lesson
    # completion.
    url: str = "redis://localhost:6379/3"


class LearningMetricsSettings(MetricsSettings):
    worker_port: int = 9103


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SMARTCOURSE_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app: LearningAppSettings = Field(default_factory=LearningAppSettings)
    log: LogSettings = Field(default_factory=LogSettings)
    cors: CorsSettings = Field(default_factory=CorsSettings)
    postgres: LearningPostgresSettings = Field(default_factory=LearningPostgresSettings)
    redis: LearningRedisSettings = Field(default_factory=LearningRedisSettings)
    health: HealthSettings = Field(default_factory=HealthSettings)
    idempotency: IdempotencySettings = Field(default_factory=IdempotencySettings)
    # Public key only — this service verifies tokens, it never issues them.
    security: JWTVerifySettings = Field(default_factory=JWTVerifySettings)
    # Used by the worker process (src/worker.py) only — the API never talks to Kafka.
    outbox: OutboxSettings = Field(default_factory=OutboxSettings)
    kafka: KafkaSettings = Field(default_factory=KafkaSettings)
    metrics: LearningMetricsSettings = Field(default_factory=LearningMetricsSettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)


@lru_cache
def get_settings() -> Settings:
    return Settings()
