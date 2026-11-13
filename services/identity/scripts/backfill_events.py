"""One-off: announce every existing account as ``user.registered.v1`` — for accounts
created before the event spine existed (week 3), which no consumer ever heard about.
Run via `make backfill`, once, with the identity worker running to deliver the rows.

Consumers absorb it safely: learning and analytics upsert by user id (analytics ignores
anything older than what it holds), and notification records — but does not send — a
welcome for an account registered more than a day ago.
"""

import asyncio

from sqlalchemy import select

from src.config import get_settings
from src.db import postgres
from src.modules.users.events import record_user_registered
from src.modules.users.models import User


async def main() -> None:
    postgres.init_postgres(get_settings().postgres)
    try:
        async with postgres.database.session_scope() as session:
            users = list((await session.execute(select(User).order_by(User.created_at))).scalars())
            for user in users:
                record_user_registered(session, user)
        print(f"identity: queued user.registered.v1 for {len(users)} account(s)")
    finally:
        await postgres.close_postgres()


if __name__ == "__main__":
    asyncio.run(main())
