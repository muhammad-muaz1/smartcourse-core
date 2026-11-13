"""Notification records: created from events, delivered by Celery, read by their owner.

    consumer (Kafka)  ──▶ INSERT notification (pending)  [inbox tx] ──after commit──▶ enqueue
    Celery task       ──▶ SELECT … FOR UPDATE; pending? send, mark sent             [1 tx]
                          retries exhausted? mark failed + outbox workflow.failed.v1 [1 tx]
    beat sweep        ──▶ re-enqueue rows still pending after stale_pending_seconds

Delivery is at-least-once: a send that succeeds but whose commit fails is retried. For a
welcome message that is the right trade — a duplicate email beats a lost one.

Every function takes the session first and never commits (CLAUDE.md §6).
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Protocol

import structlog
from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.exceptions import NotFoundError
from platform_core.outbox import add_outbox_message
from platform_core.response import CursorPage, decode_cursor, encode_cursor
from smartcourse_contracts.enrollment_events import EnrollmentEventV1
from smartcourse_contracts.platform_events import (
    WORKFLOW_FAILED_TOPIC,
    WorkflowFailedEventV1,
    WorkflowFailureV1,
)
from smartcourse_contracts.user_events import UserEventV1
from src.db.outbox import OutboxMessage
from src.modules.notifications.models import (
    DeliveryStatus,
    Notification,
    NotificationKind,
    Recipient,
)
from src.modules.notifications.schemas import NotificationPublic

logger = structlog.get_logger(__name__)

DELIVERY_WORKFLOW = "notification.delivery"


class NotificationNotFoundError(NotFoundError):
    code = "NOTIFICATION_NOT_FOUND"
    message = "Notification not found"


class RecipientUnknownError(Exception):
    """The user's registration event hasn't arrived yet. Retryable: it usually has by
    the next attempt."""


class Mailer(Protocol):
    async def send(
        self, *, to: str, subject: str, body: str, notification_id: uuid.UUID
    ) -> None: ...


class LogMailer:
    """Writes the delivery to the structured log instead of an SMTP relay. Logs the
    notification id, never the address or body — those are personal data (CLAUDE.md §11)."""

    async def send(self, *, to: str, subject: str, body: str, notification_id: uuid.UUID) -> None:
        await logger.ainfo(
            "notification_delivered", channel="log", notification_id=str(notification_id)
        )


def _now() -> datetime:
    return datetime.now(tz=UTC)


# --- from events ---------------------------------------------------------------------


async def upsert_recipient(session: AsyncSession, event: UserEventV1) -> None:
    user = event.user
    values = {"email": user.email, "full_name": user.full_name, "synced_at": _now()}
    await session.execute(
        insert(Recipient)
        .values(user_id=user.user_id, **values)
        .on_conflict_do_update(index_elements=[Recipient.user_id], set_=values)
    )


async def _create(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    kind: NotificationKind,
    subject: str,
    body: str,
    source_event_id: uuid.UUID,
    happened_at: datetime,
    max_age: timedelta,
) -> uuid.UUID | None:
    """The new notification's id if it should be delivered; None if it already existed
    or is too old to send (recorded as skipped)."""
    stale = _now() - happened_at > max_age
    created = await session.scalar(
        insert(Notification)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            kind=kind,
            subject=subject,
            body=body,
            source_event_id=source_event_id,
            status=DeliveryStatus.SKIPPED if stale else DeliveryStatus.PENDING,
            created_at=_now(),
            updated_at=_now(),
        )
        .on_conflict_do_nothing(constraint="uq_notifications_source_event_kind")
        .returning(Notification.id)
    )
    return None if created is None or stale else created


async def notify_account_created(
    session: AsyncSession, event: UserEventV1, *, max_age: timedelta
) -> uuid.UUID | None:
    user = event.user
    return await _create(
        session,
        user_id=user.user_id,
        kind=NotificationKind.ACCOUNT_WELCOME,
        subject="Welcome to SmartCourse",
        body=f"Hi {user.full_name}, your SmartCourse account is ready.",
        source_event_id=event.event_id,
        happened_at=user.registered_at,
        max_age=max_age,
    )


async def notify_enrolled(
    session: AsyncSession, event: EnrollmentEventV1, *, max_age: timedelta
) -> uuid.UUID | None:
    enrollment = event.enrollment
    return await _create(
        session,
        user_id=enrollment.student_id,
        kind=NotificationKind.COURSE_WELCOME,
        subject=f"Welcome to {enrollment.course_title}",
        body=f"You're enrolled in {enrollment.course_title}. Your progress starts now.",
        source_event_id=event.event_id,
        happened_at=enrollment.enrolled_at,
        max_age=max_age,
    )


async def notify_completed(
    session: AsyncSession, event: EnrollmentEventV1, *, max_age: timedelta
) -> uuid.UUID | None:
    enrollment = event.enrollment
    return await _create(
        session,
        user_id=enrollment.student_id,
        kind=NotificationKind.COURSE_COMPLETED,
        subject=f"You completed {enrollment.course_title}",
        body=(
            f"Congratulations — you finished all {enrollment.lessons_total} lessons of "
            f"{enrollment.course_title}."
        ),
        source_event_id=event.event_id,
        happened_at=enrollment.completed_at or event.occurred_at,
        max_age=max_age,
    )


# --- delivery (Celery) ---------------------------------------------------------------


async def deliver(session: AsyncSession, notification_id: uuid.UUID, *, mailer: Mailer) -> str:
    """One delivery attempt. Returns the resulting status. Safe to repeat: anything but
    ``pending`` is already settled and returned as-is."""
    notification = (
        await session.execute(
            select(Notification).where(Notification.id == notification_id).with_for_update()
        )
    ).scalar_one_or_none()
    if notification is None:
        raise NotificationNotFoundError()
    if notification.status != DeliveryStatus.PENDING:
        return notification.status.value
    recipient = await session.get(Recipient, notification.user_id)
    if recipient is None:
        raise RecipientUnknownError(f"no recipient record for user {notification.user_id}")

    await mailer.send(
        to=recipient.email,
        subject=notification.subject,
        body=notification.body,
        notification_id=notification.id,
    )
    notification.status = DeliveryStatus.SENT
    notification.sent_at = _now()
    notification.attempts += 1
    notification.last_error = None
    return notification.status.value


async def record_failed_attempt(
    session: AsyncSession, notification_id: uuid.UUID, *, error: str, final: bool
) -> None:
    """Every failed attempt is visible on the row; the last one also fails the row and
    reports ``workflow.failed.v1`` for analytics, in the same transaction."""
    notification = (
        await session.execute(
            select(Notification).where(Notification.id == notification_id).with_for_update()
        )
    ).scalar_one_or_none()
    if notification is None or notification.status != DeliveryStatus.PENDING:
        return
    notification.attempts += 1
    notification.last_error = error[:2000]
    if not final:
        return
    notification.status = DeliveryStatus.FAILED
    event = WorkflowFailedEventV1(
        event_id=uuid.uuid4(),
        event_type="workflow.failed.v1",
        occurred_at=_now(),
        failure=WorkflowFailureV1(
            service="notification",
            workflow=DELIVERY_WORKFLOW,
            reference_id=str(notification.id),
            failed_step="deliver",
            error=notification.last_error,
        ),
    )
    add_outbox_message(
        session,
        OutboxMessage,
        message_id=event.event_id,
        topic=WORKFLOW_FAILED_TOPIC,
        aggregate_id=notification.id,
        payload=event.model_dump(mode="json"),
    )


async def stale_pending_ids(
    session: AsyncSession, *, older_than: timedelta, limit: int = 500
) -> list[uuid.UUID]:
    """Pending rows untouched for ``older_than`` — their enqueue was lost (broker down
    after the consumer committed) or their task died with its worker."""
    cutoff = _now() - older_than
    rows = await session.execute(
        select(Notification.id)
        .where(Notification.status == DeliveryStatus.PENDING, Notification.updated_at < cutoff)
        .order_by(Notification.created_at)
        .limit(limit)
    )
    return list(rows.scalars())


# --- inbox (HTTP) --------------------------------------------------------------------


def _public(notification: Notification) -> NotificationPublic:
    return NotificationPublic.model_validate(notification, from_attributes=True)


async def list_for_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    unread_only: bool,
    limit: int,
    cursor: str | None,
) -> CursorPage[NotificationPublic]:
    """Keyset pagination on (created_at DESC, id DESC). Skipped notifications are shown:
    they are real facts, only their delivery was suppressed."""
    stmt = (
        select(Notification)
        .where(Notification.user_id == user_id)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
    )
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    if cursor:
        position = decode_cursor(cursor)
        at = datetime.fromisoformat(position["created_at"])
        last_id = uuid.UUID(position["id"])
        stmt = stmt.where(
            or_(
                Notification.created_at < at,
                and_(Notification.created_at == at, Notification.id < last_id),
            )
        )
    rows = list((await session.execute(stmt.limit(limit + 1))).scalars())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        encode_cursor({"created_at": rows[-1].created_at.isoformat(), "id": str(rows[-1].id)})
        if has_more and rows
        else None
    )
    return CursorPage(items=[_public(n) for n in rows], next_cursor=next_cursor, has_more=has_more)


async def mark_read(
    session: AsyncSession, notification_id: uuid.UUID, *, user_id: uuid.UUID
) -> NotificationPublic:
    """Idempotent. Someone else's notification is a 404, not a 403."""
    notification = await session.get(Notification, notification_id)
    if notification is None or notification.user_id != user_id:
        raise NotificationNotFoundError()
    if notification.read_at is None:
        notification.read_at = _now()
        await session.flush()
    return _public(notification)


async def mark_all_read(session: AsyncSession, *, user_id: uuid.UUID) -> int:
    result = await session.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=_now())
        .returning(Notification.id)
    )
    return len(result.all())
