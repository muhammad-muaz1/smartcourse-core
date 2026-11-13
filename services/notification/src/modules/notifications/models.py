"""Notification records and the recipient directory they are delivered to.

Records are written only by event consumers — there is no send endpoint, ever (API
design §9). A record is the durable fact "this user should be told X"; the Celery task
is the retryable attempt to tell them. So a broker outage loses no notification: the
row stays ``pending`` and the sweep re-enqueues it.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base, TimestampMixin, UUIDMixin


def _enum(enum_cls: type[StrEnum], name: str) -> Enum:
    # values_callable: store .value, not .name (docs/adr/0008).
    return Enum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda members: [member.value for member in members],
    )


class NotificationKind(StrEnum):
    ACCOUNT_WELCOME = "account_welcome"
    COURSE_WELCOME = "course_welcome"
    COURSE_COMPLETED = "course_completed"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    # Recorded for the inbox but never delivered: the fact was too old to greet anyone
    # about (a replay or backfill), see DeliverySettings.max_age_hours.
    SKIPPED = "skipped"


class Recipient(Base):
    """Where to reach a user, from ``user.registered.v1``. This service is the only one
    that keeps the email address — it is the one that delivers."""

    __tablename__ = "recipients"

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(255))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Notification(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "notifications"
    __table_args__ = (
        # One notification of a kind per source event: a replayed event, even one that
        # slipped past the inbox, can't notify twice.
        UniqueConstraint("source_event_id", "kind", name="uq_notifications_source_event_kind"),
        Index("ix_notifications_user_id_created_at", "user_id", "created_at"),
        # The sweep's scan: pending rows only.
        Index(
            "ix_notifications_pending",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    # An identity user id: plain UUID, no foreign key across services.
    user_id: Mapped[uuid.UUID]
    kind: Mapped[NotificationKind] = mapped_column(_enum(NotificationKind, "notification_kind"))
    subject: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[DeliveryStatus] = mapped_column(
        _enum(DeliveryStatus, "delivery_status"), default=DeliveryStatus.PENDING
    )
    source_event_id: Mapped[uuid.UUID]
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
