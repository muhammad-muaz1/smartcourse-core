"""The ``assets`` table: metadata for an uploaded file. The bytes live in object storage
(storage.py) under ``storage_key``; this row is the access-control anchor — a download is
authorised against the asset's course, exactly like a lesson.

A new module alongside ``courses`` (the root CLAUDE.md keeps that one deliberately
single): assets are a distinct concern — binary storage — and reference courses and
lessons only by id, through ``courses``' service helpers, never by importing its write
internals.
"""

import uuid

from sqlalchemy import BigInteger, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base, TimestampMixin, UUIDMixin


class Asset(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "assets"

    # Deleting the course (or the lesson) takes its assets' rows with it; the stored
    # bytes are swept separately — a dropped row only orphans a file, never the reverse.
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), index=True
    )
    lesson_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("lessons.id", ondelete="CASCADE"), index=True, default=None
    )
    # The name the user uploaded, shown back to them and sent on download. Never used to
    # build the storage path (storage_key is UUID-based), so a crafted name is inert.
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    # Opaque key into the storage backend: "<course_id>/<asset_id><ext>". Unique so two
    # rows can never point at one object.
    storage_key: Mapped[str] = mapped_column(String(512), unique=True)
    # Hex sha256 of the bytes — integrity check and a cheap dedup key if we add one.
    checksum_sha256: Mapped[str] = mapped_column(String(64))
    # identity user id (owning instructor or admin). A plain UUID, no FK (CLAUDE.md).
    uploaded_by: Mapped[uuid.UUID] = mapped_column()
