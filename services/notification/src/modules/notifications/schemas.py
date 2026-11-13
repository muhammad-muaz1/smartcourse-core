"""Pydantic response models for the notification inbox."""

import uuid
from datetime import datetime

from pydantic import BaseModel

from src.modules.notifications.models import DeliveryStatus, NotificationKind


class NotificationPublic(BaseModel):
    id: uuid.UUID
    kind: NotificationKind
    subject: str
    body: str
    status: DeliveryStatus
    created_at: datetime
    sent_at: datetime | None
    read_at: datetime | None


class ReadAllResult(BaseModel):
    marked_read: int
