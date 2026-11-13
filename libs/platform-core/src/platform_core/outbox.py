"""Transactional outbox (CLAUDE.md §7): the table every producing service owns, the writer
that records a message in the caller's transaction, and the relay that delivers it.

A state change that other services must hear about writes its message in the **same
transaction** as the change, so the two commit together or not at all. Nothing on a
request path talks to a broker; the relay — in the service's worker process — is the
only thing that delivers.

Each service declares its own table on its own ``Base``::

    class OutboxMessage(Base, OutboxMixin): ...

The writer captures the request id and the current trace context into ``headers``, so
the relay can continue the request's trace when it delivers minutes later.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog
from opentelemetry import trace
from sqlalchemy import DateTime, Index, Integer, String, Text, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from platform_core.config import OutboxSettings
from platform_core.db import PostgresDatabase
from platform_core.logging import bind_request_id, clear_request_context, current_request_id
from platform_core.metrics import (
    OUTBOX_BACKLOG,
    OUTBOX_DELIVERED,
    OUTBOX_FAILURES,
    OUTBOX_OLDEST_AGE,
)
from platform_core.telemetry import extract, inject

logger = structlog.get_logger(__name__)
tracer = trace.get_tracer(__name__)

REQUEST_ID_HEADER = "request_id"


class OutboxMixin:
    __tablename__ = "outbox_messages"

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Any, ...]:
        # The relay only ever scans undelivered rows; keep that index tiny.
        return (
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
    # The partition key: every message about one aggregate lands on one partition.
    aggregate_id: Mapped[uuid.UUID]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # request_id + W3C trace context, captured when the row was written.
    headers: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(tz=UTC)
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text, default=None)


def add_outbox_message[M: OutboxMixin](
    session: AsyncSession,
    model: type[M],
    *,
    message_id: uuid.UUID,
    topic: str,
    aggregate_id: uuid.UUID,
    payload: dict[str, Any],
) -> M:
    headers = inject()
    request_id = current_request_id()
    if request_id:
        headers[REQUEST_ID_HEADER] = request_id
    # Attributes set after construction: the mixin carries no __init__ signature that a
    # type checker can see, only the concrete mapped class does.
    message = model()
    message.id = message_id
    message.topic = topic
    message.aggregate_id = aggregate_id
    message.payload = payload
    message.headers = headers
    session.add(message)
    return message


Deliver = Callable[[OutboxMixin], Awaitable[None]]


class OutboxRelay:
    """Polls undelivered rows with ``FOR UPDATE SKIP LOCKED`` — so several relays can run
    side by side without delivering a row twice concurrently — delivers each, and stamps
    ``published_at`` in the same transaction. At-least-once: a crash after delivery but
    before commit redelivers, which consumers absorb through their inbox.

    On a failure the batch stops, so rows behind it keep their order, and the loop
    backs off: a broker outage costs a slow retry loop, not a hot one.
    """

    def __init__(
        self,
        *,
        database: PostgresDatabase,
        model: type[OutboxMixin],
        deliver: Deliver,
        settings: OutboxSettings,
        max_backoff_seconds: float = 30.0,
    ) -> None:
        self._database = database
        self._model = model
        self._deliver = deliver
        self._settings = settings
        self._max_backoff = max_backoff_seconds

    async def run_once(self) -> tuple[int, bool]:
        """One batch. Returns (rows delivered, whether a delivery failed)."""
        model = self._model
        delivered = 0
        async with self._database.session_scope() as session:
            result = await session.execute(
                select(model)
                .where(model.published_at.is_(None))
                .order_by(model.created_at)
                .limit(self._settings.batch_size)
                .with_for_update(skip_locked=True)
            )
            for message in result.scalars():
                if not await self._deliver_one(message):
                    return delivered, True
                delivered += 1
            await self._record_backlog(session)
        return delivered, False

    async def _deliver_one(self, message: OutboxMixin) -> bool:
        request_id = message.headers.get(REQUEST_ID_HEADER)
        if request_id:
            bind_request_id(request_id)
        try:
            # Continues the trace of the request that wrote the row.
            with tracer.start_as_current_span(
                f"{message.topic} publish",
                context=extract(message.headers),
                kind=trace.SpanKind.PRODUCER,
                attributes={
                    "messaging.destination.name": message.topic,
                    "messaging.message.id": str(message.id),
                },
            ):
                await self._deliver(message)
        except Exception as exc:  # noqa: BLE001 - any delivery failure → retry later
            message.attempts += 1
            message.last_error = str(exc)[:2000]
            OUTBOX_FAILURES.labels(topic=message.topic).inc()
            await logger.awarning(
                "outbox_delivery_failed",
                topic=message.topic,
                id=str(message.id),
                attempts=message.attempts,
                detail=str(exc),
            )
            return False
        finally:
            clear_request_context()
        message.published_at = datetime.now(tz=UTC)
        OUTBOX_DELIVERED.labels(topic=message.topic).inc()
        return True

    async def _record_backlog(self, session: AsyncSession) -> None:
        model = self._model
        count, oldest = (
            await session.execute(
                select(func.count(), func.min(model.created_at)).where(model.published_at.is_(None))
            )
        ).one()
        OUTBOX_BACKLOG.set(count)
        age = (datetime.now(tz=UTC) - oldest).total_seconds() if oldest is not None else 0.0
        OUTBOX_OLDEST_AGE.set(age)

    async def run_forever(self) -> None:
        poll = self._settings.poll_interval_seconds
        backoff = poll
        await logger.ainfo("outbox_relay_started", table=self._model.__tablename__)
        while True:
            try:
                delivered, failed = await self.run_once()
            except Exception as exc:  # noqa: BLE001 - e.g. Postgres down; keep the loop alive
                await logger.awarning("outbox_relay_error", detail=str(exc))
                delivered, failed = 0, True

            if failed:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff)
                continue
            backoff = poll
            if delivered < self._settings.batch_size:
                await asyncio.sleep(poll)
