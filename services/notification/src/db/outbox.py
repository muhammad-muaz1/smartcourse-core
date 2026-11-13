"""notification_db's transactional outbox (CLAUDE.md §7; mechanics in platform_core.outbox).
Today it carries one thing: ``workflow.failed.v1`` when a delivery exhausts its retries,
written in the same transaction that marks the notification failed."""

from platform_core.outbox import OutboxMixin
from src.db.base import Base


class OutboxMessage(Base, OutboxMixin):
    pass
