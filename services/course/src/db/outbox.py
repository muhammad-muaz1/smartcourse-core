"""course_db's transactional outbox (CLAUDE.md §7; mechanics in platform_core.outbox).

A change other services must hear about writes its message here in the same transaction
as the change. The worker process's relay is the only thing that delivers it: Kafka for
``smartcourse.*`` event topics, Temporal for the internal publish-requested message.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.outbox import OutboxMixin
from platform_core.outbox import add_outbox_message as _add
from src.db.base import Base


class OutboxMessage(Base, OutboxMixin):
    pass


def add_outbox_message(
    session: AsyncSession,
    *,
    message_id: uuid.UUID,
    topic: str,
    aggregate_id: uuid.UUID,
    payload: dict[str, Any],
) -> OutboxMessage:
    return _add(
        session,
        OutboxMessage,
        message_id=message_id,
        topic=topic,
        aggregate_id=aggregate_id,
        payload=payload,
    )
