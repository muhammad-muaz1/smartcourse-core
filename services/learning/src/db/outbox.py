"""learning_db's transactional outbox (CLAUDE.md §7; mechanics in platform_core.outbox).
``enrollment.*.v1`` rows are written in the enrollment's own transaction and delivered to
Kafka by the relay in the worker process."""

from platform_core.outbox import OutboxMixin
from src.db.base import Base


class OutboxMessage(Base, OutboxMixin):
    pass
