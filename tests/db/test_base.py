"""Pure structural checks — no I/O. mapped_column's Python-side defaults only apply at
flush time, not at object construction, so verifying they actually produce a UUID/
timestamp on insert belongs in tests/db/test_postgres.py, which does a real insert
against a real database anyway.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base, TimestampMixin, UUIDMixin


class _Widget(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "test_base_widgets"

    name: Mapped[str] = mapped_column()


def test_id_column_is_the_primary_key() -> None:
    id_column = _Widget.__table__.columns["id"]
    assert id_column.primary_key is True
    assert id_column.type.python_type is UUID


def test_id_column_has_a_default_generator() -> None:
    assert _Widget.__table__.columns["id"].default is not None


def test_timestamp_columns_are_datetime_typed_with_defaults() -> None:
    columns = _Widget.__table__.columns
    assert columns["created_at"].type.python_type is datetime
    assert columns["updated_at"].type.python_type is datetime
    assert columns["created_at"].default is not None
    assert columns["updated_at"].onupdate is not None
