"""One-off move from the monolith (single database, repo-root .env) to the services.
Run via `make cutover`; safe to re-run. Delete this file once every developer's local
setup has been cut over — nothing else imports it.

    python -m scripts.cutover_from_monolith env    # repo-root .env -> per-service .env
    python -m scripts.cutover_from_monolith data   # legacy DB rows -> identity_db, course_db

`env` never overwrites a per-service .env that already exists. `data` only reads the
legacy database and inserts with ON CONFLICT DO NOTHING, so the monolith's database is
left exactly as it was and a second run changes nothing. Run `data` after the services'
migrations (`make migrate`).

This is the only code that reads the legacy database. It is not a service reading
another service's database: the monolith's database belongs to no service, and after
cutover nothing reads it at all.
"""

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path

import asyncpg
from dotenv import dotenv_values
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parent.parent
LEGACY_ENV = ROOT / ".env"
PREFIX = "SMARTCOURSE_"

# Per target: which legacy settings carry over, and which database it gets. Service
# name, port and key paths are deliberately never copied — each service's own defaults
# are the right values now, and the monolith's would collide (every service on :8000).
_COMMON = ("APP__ENV", "APP__DEBUG", "APP__RELOAD", "APP__HOST", "APP__API_PREFIX")
_COMMON_GROUPS = ("LOG__", "CORS__", "HEALTH__")
_NEVER = ("APP__SERVICE_NAME", "APP__PORT", "MONGO__")


@dataclass(frozen=True)
class Target:
    database: str | None = None
    groups: tuple[str, ...] = ()
    extra_keys: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


TARGETS: dict[str, Target] = {
    "services/identity": Target(
        database="identity_db",
        groups=("POSTGRES__", "REDIS__", "SECURITY__"),
        exclude=("SECURITY__JWT_PRIVATE_KEY_PATH", "SECURITY__JWT_PUBLIC_KEY_PATH"),
    ),
    "services/course": Target(
        database="course_db",
        groups=("POSTGRES__",),
        extra_keys=("SECURITY__JWT_ALGORITHM",),
    ),
    "gateway": Target(),
}


def _legacy_settings() -> dict[str, str]:
    if not LEGACY_ENV.exists():
        raise SystemExit("No repo-root .env — nothing to cut over. Use `make env` instead.")
    values = dotenv_values(LEGACY_ENV)
    return {k[len(PREFIX) :]: v for k, v in values.items() if k.startswith(PREFIX) and v}


def _wanted(key: str, target: Target) -> bool:
    if key.startswith(_NEVER) or key in target.exclude:
        return False
    return (
        key in _COMMON or key.startswith(_COMMON_GROUPS + target.groups) or key in target.extra_keys
    )


def split_env() -> None:
    legacy = _legacy_settings()
    for directory, target in TARGETS.items():
        env_path = ROOT / directory / ".env"
        if env_path.exists():
            print(f"{directory}/.env already exists — leaving it alone.")
            continue

        lines = [
            "# Generated from the monolith's .env by scripts/cutover_from_monolith.py.",
            f"# See {directory}/env.example for every setting this service reads.",
        ]
        for key, value in legacy.items():
            if not _wanted(key, target):
                continue
            if key == "POSTGRES__DSN" and target.database:
                url = make_url(value).set(database=target.database)
                value = url.render_as_string(hide_password=False)
            lines.append(f"{PREFIX}{key}={value}")

        env_path.write_text("\n".join(lines) + "\n")
        env_path.chmod(0o600)
        print(f"Wrote {directory}/.env")


def _asyncpg_dsn(dsn: str) -> str:
    return make_url(dsn).set(drivername="postgresql").render_as_string(hide_password=False)


def _service_dsn(directory: str) -> str:
    dsn = dotenv_values(ROOT / directory / ".env").get(f"{PREFIX}POSTGRES__DSN")
    if not dsn:
        raise SystemExit(f"No {PREFIX}POSTGRES__DSN in {directory}/.env — run the env step first")
    return _asyncpg_dsn(dsn)


async def _copy(
    source: asyncpg.Connection, target: asyncpg.Connection, table: str, columns: list[str]
) -> int:
    column_list = ", ".join(columns)
    rows = await source.fetch(f"SELECT {column_list} FROM {table}")
    placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
    await target.executemany(
        f"INSERT INTO {table} ({column_list}) VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING",
        [tuple(row) for row in rows],
    )
    return len(rows)


async def copy_data() -> None:
    legacy_dsn = _legacy_settings().get("POSTGRES__DSN")
    if not legacy_dsn:
        raise SystemExit("The repo-root .env has no POSTGRES__DSN for the legacy database")

    user_columns = ["id", "email", "hashed_password", "full_name", "role", "is_active"]
    token_columns = ["id", "user_id", "family_id", "token_hash", "expires_at", "revoked_at"]
    timestamps = ["created_at", "updated_at"]
    course_columns = ["id", "title", "description", "level", "status", "instructor_id"]

    legacy = await asyncpg.connect(_asyncpg_dsn(legacy_dsn))
    identity = await asyncpg.connect(_service_dsn("services/identity"))
    course = await asyncpg.connect(_service_dsn("services/course"))
    try:
        async with identity.transaction():
            users = await _copy(legacy, identity, "users", user_columns + timestamps)
            tokens = await _copy(legacy, identity, "refresh_tokens", token_columns + timestamps)
            # replaced_by_id points at another refresh_tokens row, so it's filled in
            # once every row exists rather than depending on insert order.
            links = await legacy.fetch(
                "SELECT id, replaced_by_id FROM refresh_tokens WHERE replaced_by_id IS NOT NULL"
            )
            await identity.executemany(
                "UPDATE refresh_tokens SET replaced_by_id = $2 WHERE id = $1",
                [tuple(link) for link in links],
            )
        async with course.transaction():
            courses = await _copy(legacy, course, "courses", course_columns + timestamps)
    except asyncpg.UndefinedTableError as exc:
        raise SystemExit(f"{exc} — run `make migrate` before copying data") from exc
    finally:
        for connection in (legacy, identity, course):
            await connection.close()

    print(f"identity_db: {users} users, {tokens} refresh tokens (existing rows skipped)")
    print(f"course_db:   {courses} courses (existing rows skipped)")
    print("The legacy database was only read from and is unchanged.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("step", choices=["env", "data"])
    args = parser.parse_args()
    if args.step == "env":
        split_env()
    else:
        asyncio.run(copy_data())


if __name__ == "__main__":
    main()
