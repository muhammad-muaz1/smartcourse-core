"""Outbox relay: the only thing in this service that delivers events (CLAUDE.md §7).

Polls ``outbox_messages`` for undelivered rows with ``FOR UPDATE SKIP LOCKED`` — so
several relays can run side by side without delivering a row twice concurrently — hands
each to its topic's dispatcher, and stamps ``published_at`` in the same transaction.
Delivery is at-least-once: a crash after dispatch but before commit redelivers, which
every dispatcher and consumer is built to absorb.

On a delivery failure the batch stops (the row keeps its attempt count and last error)
and the loop backs off, so a Temporal outage costs a slow retry loop, not a hot one.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select

from src.db.outbox import OutboxMessage
from src.db.postgres import database

logger = structlog.get_logger(__name__)

Dispatcher = Callable[[Any, OutboxMessage], Awaitable[None]]


async def relay_once(
    context: Any, dispatchers: Mapping[str, Dispatcher], *, batch_size: int
) -> tuple[int, bool]:
    """One batch. Returns (rows delivered, whether a delivery failed)."""
    delivered = 0
    async with database.session_scope() as session:
        result = await session.execute(
            select(OutboxMessage)
            .where(OutboxMessage.published_at.is_(None))
            .order_by(OutboxMessage.created_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        for message in result.scalars():
            dispatcher = dispatchers.get(message.topic)
            if dispatcher is None:
                # A topic with no dispatcher is a deploy mistake, not a transient
                # failure. Record it and move on so it can't block the rows behind it.
                message.attempts += 1
                message.last_error = f"no dispatcher for topic {message.topic}"
                await logger.aerror("outbox_no_dispatcher", topic=message.topic, id=str(message.id))
                continue
            try:
                await dispatcher(context, message)
            except Exception as exc:  # noqa: BLE001 - any delivery failure → retry later
                message.attempts += 1
                message.last_error = str(exc)[:2000]
                await logger.awarning(
                    "outbox_delivery_failed",
                    topic=message.topic,
                    id=str(message.id),
                    attempts=message.attempts,
                    detail=str(exc),
                )
                return delivered, True
            message.published_at = datetime.now(tz=UTC)
            delivered += 1
    return delivered, False


async def run_relay(
    context: Any,
    dispatchers: Mapping[str, Dispatcher],
    *,
    poll_interval_seconds: float,
    batch_size: int,
    max_backoff_seconds: float = 30.0,
) -> None:
    backoff = poll_interval_seconds
    await logger.ainfo("outbox_relay_started", topics=sorted(dispatchers))
    while True:
        try:
            delivered, failed = await relay_once(context, dispatchers, batch_size=batch_size)
        except Exception as exc:  # noqa: BLE001 - e.g. Postgres down; keep the loop alive
            await logger.awarning("outbox_relay_error", detail=str(exc))
            delivered, failed = 0, True

        if failed:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff_seconds)
            continue
        backoff = poll_interval_seconds
        if delivered < batch_size:
            await asyncio.sleep(poll_interval_seconds)
