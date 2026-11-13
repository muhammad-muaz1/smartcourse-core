"""Notification records from events, delivery attempts, failure reporting, the inbox.
Requires Docker (testcontainers) or SMARTCOURSE_TEST_POSTGRES_URL."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from smartcourse_contracts.platform_events import WorkflowFailedEventV1
from src.db import postgres
from src.db.outbox import OutboxMessage
from src.modules.notifications import service
from src.modules.notifications.models import DeliveryStatus, Notification
from tests.notifications.helpers import MAX_AGE, enrollment_event, user_event


class RecordingMailer:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    async def send(self, *, to: str, subject: str, body: str, notification_id: uuid.UUID) -> None:
        if self.fail:
            raise ConnectionError("smtp relay down")
        self.sent.append((to, subject))


async def _registered(
    event_kwargs: dict[str, object] | None = None,
) -> tuple[uuid.UUID, uuid.UUID | None]:
    event = user_event(**(event_kwargs or {}))  # type: ignore[arg-type]
    async with postgres.database.session_scope() as session:
        await service.upsert_recipient(session, event)
        created = await service.notify_account_created(session, event, max_age=MAX_AGE)
    return event.user.user_id, created


async def _get(notification_id: uuid.UUID) -> Notification:
    async with postgres.database.session_scope() as session:
        found = await session.get(Notification, notification_id)
        assert found is not None
        return found


async def _deliver(notification_id: uuid.UUID, mailer: RecordingMailer) -> str:
    async with postgres.database.session_scope() as session:
        return await service.deliver(session, notification_id, mailer=mailer)


class TestFromEvents:
    async def test_a_new_account_gets_a_pending_welcome(self, _db: None) -> None:
        _, created = await _registered()
        assert created is not None
        assert (await _get(created)).status == DeliveryStatus.PENDING

    async def test_the_same_event_never_creates_two_notifications(self, _db: None) -> None:
        event = user_event()
        async with postgres.database.session_scope() as session:
            first = await service.notify_account_created(session, event, max_age=MAX_AGE)
            again = await service.notify_account_created(session, event, max_age=MAX_AGE)
        assert first is not None and again is None

    async def test_an_old_fact_is_recorded_but_never_sent(self, _db: None) -> None:
        """A replay or backfill must not greet someone who registered last month."""
        user_id, created = await _registered({"registered_ago": timedelta(days=30)})
        assert created is None
        async with postgres.database.session_scope() as session:
            [row] = (
                await session.execute(select(Notification).where(Notification.user_id == user_id))
            ).scalars()
        assert row.status == DeliveryStatus.SKIPPED

    async def test_enrolment_and_completion_are_announced(self, _db: None) -> None:
        student = uuid.uuid4()
        async with postgres.database.session_scope() as session:
            welcome = await service.notify_enrolled(
                session, enrollment_event(student), max_age=MAX_AGE
            )
            done = await service.notify_completed(
                session, enrollment_event(student, "enrollment.completed.v1"), max_age=MAX_AGE
            )
        assert welcome is not None and done is not None
        assert (await _get(welcome)).subject == "Welcome to Distributed Systems"
        assert "all 4 lessons" in (await _get(done)).body


class TestDelivery:
    async def test_delivery_sends_once_and_is_safe_to_repeat(self, _db: None) -> None:
        _, created = await _registered()
        assert created is not None
        mailer = RecordingMailer()
        assert await _deliver(created, mailer) == "sent"
        assert await _deliver(created, mailer) == "sent"  # redelivered task: no-op
        assert mailer.sent == [("ada@example.com", "Welcome to SmartCourse")]

    async def test_an_unknown_recipient_is_a_retryable_failure(self, _db: None) -> None:
        student = uuid.uuid4()
        async with postgres.database.session_scope() as session:
            created = await service.notify_enrolled(
                session, enrollment_event(student), max_age=MAX_AGE
            )
        assert created is not None
        with pytest.raises(service.RecipientUnknownError):
            await _deliver(created, RecordingMailer())

    async def test_giving_up_fails_the_row_and_reports_it_in_one_transaction(
        self, _db: None
    ) -> None:
        _, created = await _registered()
        assert created is not None
        with pytest.raises(ConnectionError):
            await _deliver(created, RecordingMailer(fail=True))
        for final in (False, True):
            async with postgres.database.session_scope() as session:
                await service.record_failed_attempt(
                    session, created, error="smtp relay down", final=final
                )

        row = await _get(created)
        assert (row.status, row.attempts) == (DeliveryStatus.FAILED, 2)
        async with postgres.database.session_scope() as session:
            [message] = (await session.execute(select(OutboxMessage))).scalars()
        report = WorkflowFailedEventV1.model_validate(message.payload)
        assert report.failure.workflow == "notification.delivery"
        assert report.failure.reference_id == str(created)

    async def test_the_sweep_finds_only_stale_pending_rows(self, _db: None) -> None:
        _, stale = await _registered()
        _, fresh = await _registered()
        async with postgres.database.session_scope() as session:
            await session.execute(
                update(Notification)
                .where(Notification.id == stale)
                .values(updated_at=Notification.updated_at - timedelta(hours=1))
            )
        async with postgres.database.session_scope() as session:
            ids = await service.stale_pending_ids(session, older_than=timedelta(minutes=10))
        assert ids == [stale] and fresh not in ids


class TestInbox:
    async def test_list_read_and_read_all_are_scoped_to_the_owner(self, _db: None) -> None:
        user_id, first = await _registered()
        async with postgres.database.session_scope() as session:
            second = await service.notify_enrolled(
                session, enrollment_event(user_id), max_age=MAX_AGE
            )
        assert first is not None and second is not None

        async with postgres.database.session_scope() as session:
            page = await service.list_for_user(
                session, user_id=user_id, unread_only=True, limit=1, cursor=None
            )
            assert page.has_more and len(page.items) == 1
            rest = await service.list_for_user(
                session, user_id=user_id, unread_only=True, limit=1, cursor=page.next_cursor
            )
            assert {page.items[0].id, rest.items[0].id} == {first, second}

            with pytest.raises(service.NotificationNotFoundError):
                await service.mark_read(session, first, user_id=uuid.uuid4())
            read = await service.mark_read(session, first, user_id=user_id)
            assert read.read_at is not None
            assert await service.mark_all_read(session, user_id=user_id) == 1
            unread = await service.list_for_user(
                session, user_id=user_id, unread_only=True, limit=10, cursor=None
            )
            assert unread.items == []
