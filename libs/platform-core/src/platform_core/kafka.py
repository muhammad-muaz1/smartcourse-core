"""Kafka mechanics: the publisher the outbox relay delivers through, topic provisioning,
and the consumer runner every consuming service uses (CLAUDE.md §7, docs/adr/0012).

Wire format: the value is the event's JSON (validated against its contract model by the
consumer); the key is the aggregate id; headers carry ``event_id``, ``request_id`` and the
W3C trace context. Every consumed message runs as::

    span "<topic> process" (parent = the producer's span, from the headers)
      BEGIN
        INSERT processed_events (event_id, group) ON CONFLICT DO NOTHING  → duplicate? skip
        handler(session, message)
      COMMIT
    commit offset

A failed handler is retried in-process with capped exponential backoff; the partition
waits meanwhile, because order within an aggregate matters more than throughput. A
message that still fails — or can never succeed (bad JSON, schema mismatch) — is parked
on the dead-letter topic with its error, and the partition moves on. Nothing is dropped
silently, and analytics counts every dead letter.
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, ConsumerRecord, TopicPartition
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from opentelemetry import trace
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.config import KafkaSettings
from platform_core.db import PostgresDatabase
from platform_core.inbox import ProcessedEventMixin, claim_event
from platform_core.logging import bind_request_id, clear_request_context
from platform_core.metrics import EVENT_HANDLE_DURATION, EVENTS_CONSUMED
from platform_core.outbox import REQUEST_ID_HEADER, OutboxMixin
from platform_core.telemetry import extract, inject
from smartcourse_contracts.platform_events import DeadLetterV1
from smartcourse_contracts.topics import DEAD_LETTER_TOPIC

logger = structlog.get_logger(__name__)
tracer = trace.get_tracer(__name__)

EVENT_ID_HEADER = "event_id"

# Event topics are kept forever: a projection bug is fixed by replaying history, and
# history that has expired can't be replayed (CLAUDE.md §7). Dead letters are evidence
# to act on, not history to rebuild from.
_RETAIN_FOREVER = {"retention.ms": "-1"}
_DEAD_LETTER_RETENTION = {"retention.ms": str(30 * 24 * 60 * 60 * 1000)}


class NonRetryableError(Exception):
    """Raised by a handler when retrying cannot change the outcome — the message goes
    straight to the dead-letter topic."""


# Retrying a message that doesn't parse can't make it parse.
_NON_RETRYABLE = (NonRetryableError, ValidationError, json.JSONDecodeError, UnicodeDecodeError)


# --- producing -----------------------------------------------------------------------


class KafkaPublisher:
    """Idempotent producer (``acks=all``): a broker-side retry never duplicates a message
    within a partition."""

    def __init__(self, settings: KafkaSettings, *, client_id: str) -> None:
        self._producer = AIOKafkaProducer(
            bootstrap_servers=settings.bootstrap_servers,
            client_id=client_id,
            acks="all",
            enable_idempotence=True,
        )

    async def start(self) -> None:
        await self._producer.start()

    async def stop(self) -> None:
        await self._producer.stop()

    async def publish(
        self, topic: str, *, key: str, value: bytes, headers: Mapping[str, str]
    ) -> None:
        await self._producer.send_and_wait(
            topic,
            value=value,
            key=key.encode(),
            headers=[(name, val.encode()) for name, val in headers.items()],
        )

    async def deliver_outbox(self, message: OutboxMixin) -> None:
        """The relay's ``Deliver`` for Kafka topics. Called inside the relay's publish
        span, so the injected trace context makes consumers children of it."""
        headers = inject({EVENT_ID_HEADER: str(message.id)})
        request_id = message.headers.get(REQUEST_ID_HEADER)
        if request_id:
            headers[REQUEST_ID_HEADER] = request_id
        await self.publish(
            message.topic,
            key=str(message.aggregate_id),
            value=json.dumps(message.payload, separators=(",", ":")).encode(),
            headers=headers,
        )


async def ensure_topics(settings: KafkaSettings, topics: list[str] | tuple[str, ...]) -> None:
    """Create any of ``topics`` that don't exist yet. Every worker calls this for the
    topics it produces or consumes, so a fresh broker needs no manual step; existing
    topics are left exactly as they are."""
    admin = AIOKafkaAdminClient(bootstrap_servers=settings.bootstrap_servers)
    await admin.start()
    try:
        existing = set(await admin.list_topics())
        missing = [topic for topic in dict.fromkeys(topics) if topic not in existing]
        if not missing:
            return
        await admin.create_topics(
            [
                NewTopic(
                    name=topic,
                    num_partitions=settings.partitions,
                    replication_factor=settings.replication_factor,
                    topic_configs=(
                        _DEAD_LETTER_RETENTION if topic == DEAD_LETTER_TOPIC else _RETAIN_FOREVER
                    ),
                )
                for topic in missing
            ]
        )
        # Another worker creating the same topic at the same moment is fine — the
        # broker answers TOPIC_ALREADY_EXISTS in the response, which isn't raised.
        await logger.ainfo("kafka_topics_created", topics=missing)
    finally:
        await admin.close()


# --- consuming -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EventMessage:
    topic: str
    partition: int
    offset: int
    key: str | None
    value: bytes
    headers: dict[str, str]

    @classmethod
    def from_record(cls, record: ConsumerRecord[bytes, bytes]) -> "EventMessage":
        return cls(
            topic=record.topic,
            partition=record.partition,
            offset=record.offset,
            key=record.key.decode() if record.key is not None else None,
            value=record.value or b"",
            headers={name: raw.decode(errors="replace") for name, raw in record.headers},
        )

    @property
    def event_id(self) -> uuid.UUID | None:
        raw = self.headers.get(EVENT_ID_HEADER)
        try:
            return uuid.UUID(raw) if raw else None
        except ValueError:
            return None

    def json(self) -> Any:
        return json.loads(self.value)


Handler = Callable[[AsyncSession, EventMessage], Awaitable[None]]
AfterCommit = Callable[[], Awaitable[None]]
_AFTER_COMMIT_KEY = "smartcourse.after_commit"


def after_commit(session: AsyncSession, callback: AfterCommit) -> None:
    """Run ``callback`` once the handler's transaction has committed — for side effects
    that must never see uncommitted state, like enqueueing a Celery task for a row the
    handler just wrote. Not run if the transaction rolls back. If the callback itself
    fails, the event is already handled: the callback's own recovery (e.g. a sweep that
    re-enqueues pending work) is responsible, not a redelivery."""
    callbacks: list[AfterCommit] = session.info.setdefault(_AFTER_COMMIT_KEY, [])
    callbacks.append(callback)


class EventConsumer:
    """One consumer group, many topics, one handler per topic. Offsets are committed only
    after a message is handled (or dead-lettered), so a crash redelivers — and the inbox
    turns the redelivery into a no-op."""

    def __init__(
        self,
        *,
        group_id: str,
        handlers: Mapping[str, Handler],
        database: PostgresDatabase,
        inbox_model: type[ProcessedEventMixin],
        settings: KafkaSettings,
        publisher: KafkaPublisher,
    ) -> None:
        if not handlers:
            raise ValueError(f"consumer group {group_id!r} has no handlers")
        self._group_id = group_id
        self._handlers = dict(handlers)
        self._database = database
        self._inbox_model = inbox_model
        self._settings = settings
        self._publisher = publisher

    @property
    def topics(self) -> list[str]:
        return sorted(self._handlers)

    async def run_forever(self) -> None:
        consumer = AIOKafkaConsumer(
            *self.topics,
            bootstrap_servers=self._settings.bootstrap_servers,
            group_id=self._group_id,
            client_id=self._group_id,
            enable_auto_commit=False,
            # A new group reads history from the start: that is what makes a new
            # projection (or a rebuilt one) complete.
            auto_offset_reset="earliest",
        )
        await consumer.start()
        await logger.ainfo("consumer_started", group=self._group_id, topics=self.topics)
        try:
            async for record in consumer:
                await self.handle(EventMessage.from_record(record))
                await consumer.commit(
                    {TopicPartition(record.topic, record.partition): record.offset + 1}
                )
        finally:
            await consumer.stop()

    async def handle(self, message: EventMessage) -> str:
        """Handle one message end to end. Returns the outcome (also counted in
        ``events_consumed_total``): applied, duplicate or dead_lettered."""
        request_id = message.headers.get(REQUEST_ID_HEADER)
        if request_id:
            bind_request_id(request_id)
        try:
            with tracer.start_as_current_span(
                f"{message.topic} process",
                context=extract(message.headers),
                kind=trace.SpanKind.CONSUMER,
                attributes={
                    "messaging.system": "kafka",
                    "messaging.destination.name": message.topic,
                    "messaging.consumer.group.name": self._group_id,
                    "messaging.kafka.offset": message.offset,
                },
            ) as span:
                outcome = await self._handle_with_retries(message)
                span.set_attribute("smartcourse.outcome", outcome)
        finally:
            clear_request_context()
        EVENTS_CONSUMED.labels(
            consumer_group=self._group_id, topic=message.topic, outcome=outcome
        ).inc()
        return outcome

    async def _handle_with_retries(self, message: EventMessage) -> str:
        handler = self._handlers[message.topic]
        backoff = self._settings.consumer_retry_backoff_seconds
        attempt = 0
        while True:
            attempt += 1
            try:
                with EVENT_HANDLE_DURATION.labels(
                    consumer_group=self._group_id, topic=message.topic
                ).time():
                    return await self._handle_once(handler, message)
            except _NON_RETRYABLE as exc:
                if message.topic == DEAD_LETTER_TOPIC:
                    # Parking a malformed dead letter on the dead-letter topic would
                    # loop forever. Log it loudly and move on.
                    await logger.aerror("dead_letter_unreadable", detail=str(exc))
                    return "dead_lettered"
                await self._dead_letter(message, exc, attempts=attempt)
                return "dead_lettered"
            except Exception as exc:  # noqa: BLE001 - any handler failure is retried, then parked
                # A dead letter is never dead-lettered again (that loops while the
                # database is down): it is retried until it lands.
                exhausted = attempt >= self._settings.consumer_max_attempts
                if exhausted and message.topic != DEAD_LETTER_TOPIC:
                    await self._dead_letter(message, exc, attempts=attempt)
                    return "dead_lettered"
                EVENTS_CONSUMED.labels(
                    consumer_group=self._group_id, topic=message.topic, outcome="retried"
                ).inc()
                await logger.awarning(
                    "event_handler_failed",
                    topic=message.topic,
                    offset=message.offset,
                    attempt=attempt,
                    detail=str(exc),
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._settings.consumer_max_backoff_seconds)

    async def _handle_once(self, handler: Handler, message: EventMessage) -> str:
        event_id = message.event_id
        async with self._database.session_scope() as session:
            if event_id is not None and not await claim_event(
                session, self._inbox_model, event_id=event_id, consumer_group=self._group_id
            ):
                return "duplicate"
            await handler(session, message)
            callbacks: list[AfterCommit] = session.info.pop(_AFTER_COMMIT_KEY, [])
        for callback in callbacks:
            try:
                await callback()
            except Exception as exc:  # noqa: BLE001 - committed already; see after_commit()
                await logger.awarning(
                    "after_commit_callback_failed", topic=message.topic, detail=str(exc)
                )
        return "applied"

    async def _dead_letter(self, message: EventMessage, exc: Exception, *, attempts: int) -> None:
        trace.get_current_span().record_exception(exc)
        trace.get_current_span().set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
        letter = DeadLetterV1(
            event_id=message.event_id,
            original_topic=message.topic,
            partition=message.partition,
            offset=message.offset,
            consumer_group=self._group_id,
            attempts=attempts,
            error=f"{type(exc).__name__}: {exc}"[:2000],
            failed_at=datetime.now(tz=UTC),
            payload=message.value.decode(errors="replace"),
        )
        await logger.aerror(
            "event_dead_lettered",
            topic=message.topic,
            offset=message.offset,
            attempts=attempts,
            detail=letter.error,
        )
        # The dead letter must land before the offset moves past the message, or the
        # message is lost. If Kafka itself is down, wait for it.
        backoff = self._settings.consumer_retry_backoff_seconds
        while True:
            try:
                await self._publisher.publish(
                    DEAD_LETTER_TOPIC,
                    key=self._group_id,
                    value=letter.model_dump_json().encode(),
                    headers=inject({EVENT_ID_HEADER: str(uuid.uuid4())}),
                )
                return
            except Exception as publish_exc:  # noqa: BLE001 - keep trying; never drop it
                await logger.awarning("dead_letter_publish_failed", detail=str(publish_exc))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._settings.consumer_max_backoff_seconds)


def merge_handlers(found: list[Mapping[str, Handler]]) -> dict[str, Handler]:
    """Merges every module's ``CONSUMERS`` (topic → handler). Two modules of one service
    handling the same topic would share one inbox claim, so the second would silently
    never run — refuse instead."""
    merged: dict[str, Handler] = {}
    for mapping in found:
        for topic, handler in mapping.items():
            if topic in merged:
                raise ValueError(f"two modules handle {topic}; one consumer per topic per service")
            merged[topic] = handler
    return merged
