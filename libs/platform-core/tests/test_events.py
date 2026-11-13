"""The event spine's guarantees, against a real Postgres (Docker or
SMARTCOURSE_TEST_POSTGRES_URL). Kafka itself is replaced by a recording publisher: what's
under test is what *our* code promises — outbox atomicity, ordered at-least-once
delivery, inbox dedupe, retries, dead-lettering, after-commit side effects.
"""

import json
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
from sqlalchemy import MetaData, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase
from testcontainers.community.postgres import PostgresContainer

from platform_core.config import KafkaSettings, OutboxSettings, PostgresSettings
from platform_core.db import NAMING_CONVENTION, PostgresDatabase
from platform_core.inbox import ProcessedEventMixin
from platform_core.kafka import (
    EVENT_ID_HEADER,
    EventConsumer,
    EventMessage,
    KafkaPublisher,
    NonRetryableError,
    after_commit,
    merge_handlers,
)
from platform_core.logging import bind_request_id
from platform_core.outbox import OutboxMixin, OutboxRelay, add_outbox_message
from smartcourse_contracts.platform_events import DeadLetterV1
from smartcourse_contracts.topics import DEAD_LETTER_TOPIC


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Outbox(Base, OutboxMixin):
    pass


class Inbox(Base, ProcessedEventMixin):
    pass


database = PostgresDatabase()


@pytest.fixture
async def _db(postgres_container: PostgresContainer) -> AsyncIterator[None]:
    database.init(PostgresSettings(dsn=postgres_container.get_connection_url()))
    async with database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield
    finally:
        async with database.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await database.close()


class RecordingPublisher(KafkaPublisher):
    """KafkaPublisher with the network replaced; deliver_outbox is the real code."""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.sent: list[tuple[str, str, bytes, dict[str, str]]] = []
        self.fail_times = fail_times

    async def publish(
        self, topic: str, *, key: str, value: bytes, headers: Mapping[str, str]
    ) -> None:
        if self.fail_times:
            self.fail_times -= 1
            raise ConnectionError("broker unreachable")
        self.sent.append((topic, key, value, dict(headers)))


_FAST = KafkaSettings(
    consumer_max_attempts=3, consumer_retry_backoff_seconds=0.0, consumer_max_backoff_seconds=0.0
)


async def _write(topic: str = "smartcourse.thing.happened.v1") -> uuid.UUID:
    async with database.session_scope() as session:
        message = add_outbox_message(
            session,
            Outbox,
            message_id=uuid.uuid4(),
            topic=topic,
            aggregate_id=uuid.uuid4(),
            payload={"n": 1},
        )
        return message.id


async def _row(message_id: uuid.UUID) -> Outbox:
    async with database.session_scope() as session:
        return (await session.execute(select(Outbox).where(Outbox.id == message_id))).scalar_one()


# --- outbox + relay --------------------------------------------------------------------


class TestOutbox:
    async def test_the_writer_captures_the_request_id(self, _db: None) -> None:
        bind_request_id("req-123")
        message_id = await _write()
        assert (await _row(message_id)).headers["request_id"] == "req-123"

    async def test_a_rolled_back_change_takes_its_message_with_it(self, _db: None) -> None:
        with pytest.raises(RuntimeError):
            async with database.session_scope() as session:
                add_outbox_message(
                    session,
                    Outbox,
                    message_id=uuid.uuid4(),
                    topic="t",
                    aggregate_id=uuid.uuid4(),
                    payload={},
                )
                raise RuntimeError("the business change failed")
        async with database.session_scope() as session:
            assert (await session.execute(select(Outbox))).first() is None

    async def test_relay_delivers_in_order_keyed_by_aggregate_and_never_twice(
        self, _db: None
    ) -> None:
        publisher = RecordingPublisher()
        relay = OutboxRelay(
            database=database,
            model=Outbox,
            deliver=publisher.deliver_outbox,
            settings=OutboxSettings(),
        )
        first, second = await _write(), await _write()
        assert await relay.run_once() == (2, False)
        assert await relay.run_once() == (0, False)

        assert [headers[EVENT_ID_HEADER] for *_, headers in publisher.sent] == [
            str(first),
            str(second),
        ]
        topic, key, value, _ = publisher.sent[0]
        assert key == str((await _row(first)).aggregate_id)
        assert json.loads(value) == {"n": 1}
        assert (await _row(first)).published_at is not None

    async def test_a_failed_delivery_stops_the_batch_and_keeps_the_row(self, _db: None) -> None:
        publisher = RecordingPublisher(fail_times=1)
        relay = OutboxRelay(
            database=database,
            model=Outbox,
            deliver=publisher.deliver_outbox,
            settings=OutboxSettings(),
        )
        first, second = await _write(), await _write()

        assert await relay.run_once() == (0, True)
        row = await _row(first)
        assert (row.published_at, row.attempts, row.last_error) == (None, 1, "broker unreachable")
        assert (await _row(second)).published_at is None  # order kept: nothing jumped ahead

        assert await relay.run_once() == (2, False)


# --- consumer ----------------------------------------------------------------------------


def _message(value: bytes = b"{}", *, topic: str = "smartcourse.thing.happened.v1") -> EventMessage:
    return EventMessage(
        topic=topic,
        partition=0,
        offset=7,
        key="k",
        value=value,
        headers={EVENT_ID_HEADER: str(uuid.uuid4()), "request_id": "req-9"},
    )


def _consumer(
    handler: Any, publisher: RecordingPublisher, *, topic: str = "smartcourse.thing.happened.v1"
) -> EventConsumer:
    return EventConsumer(
        group_id="tests",
        handlers={topic: handler},
        database=database,
        inbox_model=Inbox,
        settings=_FAST,
        publisher=publisher,
    )


class TestConsumer:
    async def test_a_redelivered_event_is_applied_once(self, _db: None) -> None:
        calls: list[int] = []

        async def handler(_session: AsyncSession, _message: EventMessage) -> None:
            calls.append(1)

        consumer = _consumer(handler, RecordingPublisher())
        message = _message()
        assert await consumer.handle(message) == "applied"
        assert await consumer.handle(message) == "duplicate"
        assert calls == [1]

    async def test_a_transient_failure_is_retried_and_rolls_back_each_attempt(
        self, _db: None
    ) -> None:
        attempts: list[int] = []

        async def flaky(session: AsyncSession, _message: EventMessage) -> None:
            attempts.append(1)
            add_outbox_message(
                session,
                Outbox,
                message_id=uuid.uuid4(),
                topic="t",
                aggregate_id=uuid.uuid4(),
                payload={},
            )
            if len(attempts) < 2:
                raise ConnectionError("database blip")

        assert await _consumer(flaky, RecordingPublisher()).handle(_message()) == "applied"
        async with database.session_scope() as session:
            rows = (await session.execute(select(Outbox))).scalars().all()
        assert (len(attempts), len(rows)) == (2, 1)  # the failed attempt left nothing behind

    async def test_exhausted_retries_park_the_message_with_its_error(self, _db: None) -> None:
        async def broken(_session: AsyncSession, _message: EventMessage) -> None:
            raise ConnectionError("still down")

        publisher = RecordingPublisher()
        message = _message(b'{"x": 1}')
        assert await _consumer(broken, publisher).handle(message) == "dead_lettered"

        [(topic, key, value, _)] = publisher.sent
        letter = DeadLetterV1.model_validate_json(value)
        assert (topic, key) == (DEAD_LETTER_TOPIC, "tests")
        assert (letter.attempts, letter.offset, letter.payload) == (3, 7, '{"x": 1}')
        assert letter.event_id == message.event_id
        assert "still down" in letter.error
        # Parked, not applied: a later redelivery may still be handled.
        async with database.session_scope() as session:
            assert (await session.execute(select(Inbox))).first() is None

    async def test_a_message_that_can_never_succeed_is_parked_without_retrying(
        self, _db: None
    ) -> None:
        attempts: list[int] = []

        async def strict(_session: AsyncSession, message: EventMessage) -> None:
            attempts.append(1)
            message.json()

        publisher = RecordingPublisher()
        assert await _consumer(strict, publisher).handle(_message(b"not json")) == "dead_lettered"
        assert len(attempts) == 1
        assert len(publisher.sent) == 1

    async def test_a_dead_letter_is_never_dead_lettered_again(self, _db: None) -> None:
        async def unreadable(_session: AsyncSession, _message: EventMessage) -> None:
            raise NonRetryableError("garbage")

        publisher = RecordingPublisher()
        consumer = _consumer(unreadable, publisher, topic=DEAD_LETTER_TOPIC)
        assert await consumer.handle(_message(topic=DEAD_LETTER_TOPIC)) == "dead_lettered"
        assert publisher.sent == []  # no loop

    async def test_after_commit_runs_only_once_the_transaction_commits(self, _db: None) -> None:
        ran: list[str] = []

        async def record(_: str) -> None:
            ran.append(_)

        async def handler(session: AsyncSession, _message: EventMessage) -> None:
            after_commit(session, lambda: record(f"attempt-{len(ran)}"))
            if not ran and not getattr(handler, "failed", False):
                handler.failed = True  # type: ignore[attr-defined]  # test-only flag
                raise ConnectionError("rolls back")

        assert await _consumer(handler, RecordingPublisher()).handle(_message()) == "applied"
        assert ran == ["attempt-0"]  # the rolled-back attempt's callback never ran


def test_two_modules_handling_one_topic_is_refused() -> None:
    async def handler(_session: AsyncSession, _message: EventMessage) -> None:
        return None

    with pytest.raises(ValueError, match="two modules handle"):
        merge_handlers([{"t": handler}, {"t": handler}])
