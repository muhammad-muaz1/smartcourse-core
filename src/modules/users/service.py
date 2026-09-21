"""User lookups and creation. Used by this module's own future routes and, today, by
the auth module — a deliberate exception to "modules never import another module's
model/service": auth cannot authenticate without the account it's authenticating
against. See docs/adr/0008.

Every function takes the session as its first argument rather than fetching one, so the
same function is callable from a route, a task, or a consumer unchanged.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.users.models import User, UserRole


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)


async def create_user(
    session: AsyncSession, *, email: str, hashed_password: str, full_name: str, role: UserRole
) -> User:
    user = User(email=email, hashed_password=hashed_password, full_name=full_name, role=role)
    session.add(user)
    await session.flush()
    return user
