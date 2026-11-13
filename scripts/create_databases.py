"""Creates each service's Postgres database if it doesn't exist yet. Run via `make db`.

Migrations create tables, not databases, so a fresh machine needs this once. Each
service's DSN is read from its own .env (falling back to its env.example), and the
database is created by connecting to the server's `postgres` maintenance database with
the same credentials. Never drops or alters anything.
"""

import asyncio
import sys
from pathlib import Path

import asyncpg
from dotenv import dotenv_values
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parent.parent
SERVICES_WITH_A_DATABASE = ("identity", "course", "learning", "notification", "analytics")
DSN_KEY = "SMARTCOURSE_POSTGRES__DSN"


def service_dsn(service: str) -> str:
    service_dir = ROOT / "services" / service
    for name in (".env", "env.example"):
        dsn = dotenv_values(service_dir / name).get(DSN_KEY)
        if dsn:
            return dsn
    raise SystemExit(f"No {DSN_KEY} in services/{service}/.env or env.example")


async def ensure_database(dsn: str) -> tuple[str, bool]:
    url = make_url(dsn)
    database = url.database
    if not database:
        raise SystemExit(f"DSN for {url.render_as_string()} names no database")
    maintenance = url.set(drivername="postgresql", database="postgres")
    connection = await asyncpg.connect(maintenance.render_as_string(hide_password=False))
    try:
        exists = await connection.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", database)
        if not exists:
            quoted = '"' + database.replace('"', '""') + '"'
            await connection.execute(f"CREATE DATABASE {quoted}")
        return database, not exists
    finally:
        await connection.close()


async def main() -> None:
    for service in SERVICES_WITH_A_DATABASE:
        try:
            database, created = await ensure_database(service_dsn(service))
        except (OSError, asyncpg.PostgresError) as exc:
            print(f"{service}: could not create database — {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        print(f"{service}: {database} {'created' if created else 'already exists'}")


if __name__ == "__main__":
    asyncio.run(main())
