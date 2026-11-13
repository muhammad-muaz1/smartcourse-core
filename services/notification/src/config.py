"""All notification settings, nested by concern. Every environment variable is prefixed
``SMARTCOURSE_`` and nested groups are joined with ``__``, e.g.
``SMARTCOURSE_CELERY__BROKER_URL=...`` sets ``settings.celery.broker_url``.

Read from this service's own ``.env`` (``services/notification/.env``) — never another
service's.
"""

from functools import lru_cache
from typing import Literal

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
    OutboxSettings,
    PostgresSettings,
    RedisSettings,
    TelemetrySettings,
)


class NotificationAppSettings(AppSettings):
    service_name: str = "notification"
    port: int = 8004


class NotificationPostgresSettings(PostgresSettings):
    dsn: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/notification_db"


class NotificationRedisSettings(RedisSettings):
    # DB 4 is notification's. Today: health only; inbox reads are cheap enough uncached.
    url: str = "redis://localhost:6379/4"


class NotificationMetricsSettings(MetricsSettings):
    # The consumer process. The Celery worker serves its own on celery.metrics_port.
    worker_port: int = 9104


class CelerySettings(BaseModel):
    # RabbitMQ, as the brief specifies (compose runs it). A machine without RabbitMQ can
    # point this at Redis for local work — see env.example and docs/adr/0012.
    broker_url: str = "amqp://guest:guest@localhost:5672//"
    # Per-attempt limits on the delivery task: soft raises inside the task so it can
    # record the failure; hard kills the child process if the soft one is ignored.
    soft_time_limit_seconds: int = 20
    time_limit_seconds: int = 30
    max_retries: int = 5
    retry_backoff_seconds: int = 5
    retry_backoff_max_seconds: int = 300
    # The sweep re-enqueues notifications still pending after this long — the recovery
    # path for an enqueue that failed after the consumer committed.
    # Longer than the longest retry backoff, so a row merely waiting on its retry is
    # not re-enqueued alongside it.
    stale_pending_seconds: int = 600
    sweep_interval_seconds: int = 60
    metrics_port: int = 9114


class DeliverySettings(BaseModel):
    # "log" writes the message to the structured log instead of sending it — the only
    # backend until an SMTP relay is provisioned (docs/adr/0012).
    backend: Literal["log"] = "log"
    from_address: str = "no-reply@smartcourse.local"
    # A welcome for something that happened longer ago than this is stale: it is
    # recorded, never sent. Stops a replay or backfill from greeting old users.
    max_age_hours: int = 24


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SMARTCOURSE_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app: NotificationAppSettings = Field(default_factory=NotificationAppSettings)
    log: LogSettings = Field(default_factory=LogSettings)
    cors: CorsSettings = Field(default_factory=CorsSettings)
    postgres: NotificationPostgresSettings = Field(default_factory=NotificationPostgresSettings)
    redis: NotificationRedisSettings = Field(default_factory=NotificationRedisSettings)
    health: HealthSettings = Field(default_factory=HealthSettings)
    # Public key only — this service verifies tokens, it never issues them.
    security: JWTVerifySettings = Field(default_factory=JWTVerifySettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    # Worker processes only — the API never talks to Kafka or the broker.
    kafka: KafkaSettings = Field(default_factory=KafkaSettings)
    outbox: OutboxSettings = Field(default_factory=OutboxSettings)
    metrics: NotificationMetricsSettings = Field(default_factory=NotificationMetricsSettings)
    celery: CelerySettings = Field(default_factory=CelerySettings)
    delivery: DeliverySettings = Field(default_factory=DeliverySettings)


@lru_cache
def get_settings() -> Settings:
    return Settings()
