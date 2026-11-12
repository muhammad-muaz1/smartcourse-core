"""Consumer inbox for learning_db (CLAUDE.md §7): every delivery is at-least-once, so
every consumer claims an event id here, in the same transaction as its effect, and skips
the event if the claim already exists.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class ProcessedEvent(Base):
    __tablename__ = "processed_events"

    event_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    consumer_group: Mapped[str] = mapped_column(String(255), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(tz=UTC)
    )


async def claim_event(session: AsyncSession, *, event_id: uuid.UUID, consumer_group: str) -> bool:
    """True if this consumer group is seeing ``event_id`` for the first time. Rolled
    back with the rest of the transaction if the handler fails, so a failed attempt
    doesn't swallow the redelivery."""
    result = await session.execute(
        insert(ProcessedEvent)
        .values(event_id=event_id, consumer_group=consumer_group, processed_at=datetime.now(tz=UTC))
        .on_conflict_do_nothing()
        .returning(ProcessedEvent.event_id)
    )
    return result.scalar_one_or_none() is not None
