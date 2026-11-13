"""notification_db's consumer inbox (CLAUDE.md §7; mechanics in platform_core.inbox)."""

from platform_core.inbox import ProcessedEventMixin
from src.db.base import Base


class ProcessedEvent(Base, ProcessedEventMixin):
    pass
