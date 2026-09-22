"""Creates an admin account directly. This is the only way to get one — self-
registration deliberately can't grant the admin role (src/modules/auth/schemas.py
restricts POST /auth/register to student/instructor).

Run via `make create-admin` or:
    python -m scripts.create_admin --email admin@example.com --full-name "Admin User"

Password is prompted interactively unless --password is given — prefer the prompt,
since a --password argument lands in your shell history.
"""

import argparse
import asyncio
import getpass
import sys

from src.config import get_settings
from src.core.exceptions import AppError
from src.core.security import hash_password
from src.db import postgres
from src.modules.users import service as users_service
from src.modules.users.models import UserRole


async def _create_admin(*, email: str, full_name: str, password: str) -> None:
    settings = get_settings()
    postgres.init_postgres(settings.postgres)
    try:
        async for session in postgres.get_session():
            if await users_service.get_by_email(session, email) is not None:
                raise AppError(f"A user with email {email!r} already exists.")
            user = await users_service.create_user(
                session,
                email=email,
                hashed_password=hash_password(password),
                full_name=full_name,
                role=UserRole.ADMIN,
            )
            print(f"Created admin {user.email} ({user.id})")
    finally:
        await postgres.close_postgres()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an admin account.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--full-name", required=True)
    parser.add_argument("--password", help="Omit to be prompted instead (recommended).")
    args = parser.parse_args()

    password = args.password or getpass.getpass("Password: ")
    if len(password) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        raise SystemExit(1)

    try:
        asyncio.run(_create_admin(email=args.email, full_name=args.full_name, password=password))
    except AppError as exc:
        print(f"Error: {exc.message}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
