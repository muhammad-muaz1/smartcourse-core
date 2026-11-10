"""Settings groups every service composes into its own ``Settings``. Each service owns
its ``config.py`` and its defaults (service name, port, database) — it subclasses a
group here to change a default rather than passing one in, because pydantic-settings
rebuilds a nested group from the environment alone: a default passed via
``default_factory`` is silently dropped the moment any ``SMARTCOURSE_APP__*`` variable
is set.
"""

import subprocess
from typing import Literal

from pydantic import BaseModel, Field

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
    service_name: str = "smartcourse"
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
    # Local dev default only — throwaway container credentials. Each service overrides
    # the database name (one database per service); a real deployment injects its own
    # DSN via its secrets manager and never commits one.
    dsn: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/postgres"
    pool_size: int = 10
    max_overflow: int = 5
    echo: bool = False


class RedisSettings(BaseModel):
    # One Redis, one DB index per service — each service overrides the index.
    url: str = "redis://localhost:6379/0"
    socket_timeout_seconds: float = 2.0


class HealthSettings(BaseModel):
    check_timeout_seconds: float = 2.0
    cache_ttl_seconds: float = 5.0


class JWTVerifySettings(BaseModel):
    """What every service needs to verify an access token offline. Only identity holds
    the private key; everyone else gets the public key and nothing more."""

    jwt_public_key_path: str = "../../secrets/jwt_public.pem"
    jwt_algorithm: str = "RS256"
