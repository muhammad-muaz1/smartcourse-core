"""``user.registered.v1`` — identity's announcement that an account exists.

Carries the snapshot each consumer needs so none of them calls identity: a display name
(learning's roster snapshot), an email address (notification's delivery address) and the
role (analytics' student/instructor counts). The email is personal data: consumers that
don't deliver mail must not store it.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from smartcourse_contracts.topics import topic_for

UserEventType = Literal["user.registered.v1"]
USER_REGISTERED_TOPIC = topic_for("user.registered.v1")


class UserSnapshotV1(BaseModel):
    user_id: uuid.UUID
    email: str
    full_name: str
    # Plain string, not identity's enum: student | instructor | admin today.
    role: str
    registered_at: datetime


class UserEventV1(BaseModel):
    event_id: uuid.UUID
    event_type: UserEventType
    occurred_at: datetime
    user: UserSnapshotV1
