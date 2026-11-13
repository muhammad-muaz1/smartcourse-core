"""identity_db's transactional outbox (CLAUDE.md §7; mechanics in platform_core.outbox).
Written in the same transaction as the change it announces; delivered to Kafka by the
relay in the worker process (``python -m src.worker``)."""

from platform_core.outbox import OutboxMixin
from src.db.base import Base


class OutboxMessage(Base, OutboxMixin):
    pass
