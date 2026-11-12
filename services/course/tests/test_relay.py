"""Requires Docker (testcontainers spins up a real Postgres)."""

import uuid
from typing import Any

from sqlalchemy import select

from src.db import postgres
from src.db.outbox import OutboxMessage, add_outbox_message
from src.relay import relay_once


async def _add(topic: str) -> uuid.UUID:
    async with postgres.database.session_scope() as session:
        message = add_outbox_message(
            session, message_id=uuid.uuid4(), topic=topic, aggregate_id=uuid.uuid4(), payload={}
        )
        return message.id


async def _row(message_id: uuid.UUID) -> OutboxMessage:
    async with postgres.database.session_scope() as session:
        result = await session.execute(select(OutboxMessage).where(OutboxMessage.id == message_id))
        return result.scalar_one()


async def test_delivered_rows_are_stamped_and_not_delivered_again(_db: None) -> None:
    delivered: list[uuid.UUID] = []

    async def dispatch(_ctx: Any, message: OutboxMessage) -> None:
        delivered.append(message.id)

    first, second = await _add("t"), await _add("t")
    assert await relay_once(None, {"t": dispatch}, batch_size=10) == (2, False)
    assert await relay_once(None, {"t": dispatch}, batch_size=10) == (0, False)
    assert delivered == [first, second]
    assert (await _row(first)).published_at is not None


async def test_a_failed_delivery_stays_pending_with_its_error_recorded(_db: None) -> None:
    async def broken(_ctx: Any, _message: OutboxMessage) -> None:
        raise ConnectionError("temporal unreachable")

    message_id = await _add("t")
    assert await relay_once(None, {"t": broken}, batch_size=10) == (0, True)
    row = await _row(message_id)
    assert row.published_at is None
    assert row.attempts == 1
    assert row.last_error == "temporal unreachable"


async def test_a_topic_without_a_dispatcher_does_not_block_the_rows_behind_it(_db: None) -> None:
    async def dispatch(_ctx: Any, _message: OutboxMessage) -> None:
        return None

    orphan = await _add("unknown")
    good = await _add("t")
    assert await relay_once(None, {"t": dispatch}, batch_size=10) == (1, False)
    assert (await _row(good)).published_at is not None
    assert (await _row(orphan)).last_error == "no dispatcher for topic unknown"
