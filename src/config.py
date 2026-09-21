"""All settings, nested by concern. Every environment variable is prefixed
``SMARTCOURSE_`` and nested groups are joined with ``__``, e.g.
``SMARTCOURSE_REDIS__URL=redis://localhost:6379/0`` sets ``settings.redis.url``.

``get_settings()`` is called eagerly at app startup (not lazily on first request) so a
misconfigured deployment fails before it accepts traffic.
"""

import subprocess
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "dev", "staging", "prod"]


def _detect_git_sha() -> str:
    """Local-dev convenience only. In a built image there's no .git directory, so
    SMARTCOURSE_APP__GIT_SHA is set from a Docker build arg instead."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=1,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return completed.stdout.strip() or "unknown"


class AppSettings(BaseModel):
    env: Environment = "local"
    debug: bool = False
    # Separate from `debug`: reload spawns a supervisor process that re-imports the app
    # on every file change — fine for a single local dev process, never safe in a
    # shared/staging environment even if that environment also has debug logging on.
    reload: bool = False
    service_name: str = "smartcourse-api"
    host: str = "0.0.0.0"
    port: int = 8000
    api_prefix: str = "/api/v1"
    git_sha: str = Field(default_factory=_detect_git_sha)
    build_time: str = "unknown"


class LogSettings(BaseModel):
    level: str = "INFO"
    json_logs: bool = True


class CorsSettings(BaseModel):
    allow_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    allow_credentials: bool = True


class PostgresSettings(BaseModel):
    # Local dev default only — no embedded secret worth protecting (throwaway
    # container credentials). A real deployment injects its own DSN via its secrets
    # manager and never commits one here.
    dsn: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/smartcourse"
    pool_size: int = 10
    max_overflow: int = 5
    echo: bool = False


class MongoSettings(BaseModel):
    url: str = "mongodb://localhost:27017"
    database: str = "smartcourse"
    # PyMongo's own default (30s) means an unreachable Mongo makes every operation
    # hang for 30s before failing. Bound it so an outage fails fast everywhere.
    server_selection_timeout_ms: int = 5000


class RedisSettings(BaseModel):
    url: str = "redis://localhost:6379/0"
    socket_timeout_seconds: float = 2.0


class HealthSettings(BaseModel):
    check_timeout_seconds: float = 2.0
    cache_ttl_seconds: float = 5.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SMARTCOURSE_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app: AppSettings = Field(default_factory=AppSettings)
    log: LogSettings = Field(default_factory=LogSettings)
    cors: CorsSettings = Field(default_factory=CorsSettings)
    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    mongo: MongoSettings = Field(default_factory=MongoSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    health: HealthSettings = Field(default_factory=HealthSettings)


@lru_cache
def get_settings() -> Settings:
    return Settings()
