"""Transactional outbox for course_db (CLAUDE.md §7).

A state change that other services must hear about writes its message here, in the
**same transaction** as the change. Nothing on a request path talks to a broker: the
relay (``src/relay.py``, inside the worker process) is the only thing that delivers, so
a change and its event commit together or not at all.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.logging import current_request_id
from src.db.base import Base


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"
    __table_args__ = (
        # The relay only ever scans undelivered rows; keep that index tiny.
        Index(
            "ix_outbox_messages_unpublished",
            "created_at",
            postgresql_where=text("published_at IS NULL"),
        ),
    )

    # Doubles as the event id, so every redelivery carries the same id for consumers'
    # inbox dedupe.
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    topic: Mapped[str] = mapped_column(String(255))
    aggregate_id: Mapped[uuid.UUID]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    headers: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(tz=UTC)
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text, default=None)


def add_outbox_message(
    session: AsyncSession,
    *,
    message_id: uuid.UUID,
    topic: str,
    aggregate_id: uuid.UUID,
    payload: dict[str, Any],
) -> OutboxMessage:
    request_id = current_request_id()
    message = OutboxMessage(
        id=message_id,
        topic=topic,
        aggregate_id=aggregate_id,
        payload=payload,
        headers={"request_id": request_id} if request_id else {},
    )
    session.add(message)
    return message
