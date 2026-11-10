"""All course settings, nested by concern. Every environment variable is prefixed
``SMARTCOURSE_`` and nested groups are joined with ``__``, e.g.
``SMARTCOURSE_POSTGRES__DSN=...`` sets ``settings.postgres.dsn``.

Read from this service's own ``.env`` (``services/course/.env``) — never another
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
    JWTVerifySettings,
    LogSettings,
    PostgresSettings,
)


class CourseAppSettings(AppSettings):
    service_name: str = "course"
    port: int = 8002


class CoursePostgresSettings(PostgresSettings):
    dsn: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/course_db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SMARTCOURSE_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app: CourseAppSettings = Field(default_factory=CourseAppSettings)
    log: LogSettings = Field(default_factory=LogSettings)
    cors: CorsSettings = Field(default_factory=CorsSettings)
    postgres: CoursePostgresSettings = Field(default_factory=CoursePostgresSettings)
    health: HealthSettings = Field(default_factory=HealthSettings)
    # Public key only — this service verifies tokens, it never issues them.
    security: JWTVerifySettings = Field(default_factory=JWTVerifySettings)


@lru_cache
def get_settings() -> Settings:
    return Settings()
