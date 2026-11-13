"""Events the users module emits, written to the outbox in the caller's transaction."""

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.outbox import add_outbox_message
from smartcourse_contracts.user_events import USER_REGISTERED_TOPIC, UserEventV1, UserSnapshotV1
from src.db.outbox import OutboxMessage
from src.modules.users.models import User


def record_user_registered(session: AsyncSession, user: User) -> None:
    event = UserEventV1(
        event_id=uuid.uuid4(),
        event_type="user.registered.v1",
        occurred_at=datetime.now(tz=UTC),
        user=UserSnapshotV1(
            user_id=user.id,
            email=user.email,
            full_name=user.full_name,
            role=user.role.value,
            registered_at=user.created_at,
        ),
    )
    add_outbox_message(
        session,
        OutboxMessage,
        message_id=event.event_id,
        topic=USER_REGISTERED_TOPIC,
        aggregate_id=user.id,
        payload=event.model_dump(mode="json"),
    )
