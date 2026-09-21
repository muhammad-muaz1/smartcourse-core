"""Base, UUIDMixin, TimestampMixin. Nothing else."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Alembic's own recommended naming convention — without it, autogenerate produces
# constraint names that vary by dialect and are unreviewable in a migration diff.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    # timezone=True is not optional: CLAUDE.md §8 requires timezone-aware UTC
    # everywhere, and the default SQLAlchemy DateTime column is naive — asyncpg
    # rejects a tz-aware Python datetime against a naive column outright, so this
    # isn't just a correctness nicety, it's the difference between working and a 500.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(tz=UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(tz=UTC),
        onupdate=lambda: datetime.now(tz=UTC),
    )
