"""Asset business logic: upload, read, list, delete. Bytes stream through storage.py;
this layer owns validation and access control.

Access control reuses ``courses``' helpers so assets obey exactly the same rules as the
lessons they hang off:
  * **upload / delete** — the owning instructor or an admin, and only while the course is
    editable (draft/failed). The course row is locked first (``get_course_for_update``),
    same as any content write.
  * **download / list** — anyone if the course is published; otherwise only the owner or
    an admin (an unpublished course's files aren't other people's business — a 404, not a
    403, mirroring ``get_lesson``).

Every function takes the session first and never commits (CLAUDE.md §6).
"""

from __future__ import annotations

import hashlib
import mimetypes
import uuid
from collections.abc import AsyncIterator

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.exceptions import NotFoundError, ValidationError
from src.modules.assets.models import Asset
from src.modules.assets.storage import Storage, StorageError
from src.modules.courses.models import Course, CourseStatus, Lesson
from src.modules.courses.service.courses import (
    can_modify,
    ensure_can_modify,
    ensure_content_editable,
    get_course_for_update,
    get_course_or_404,
)

logger = structlog.get_logger(__name__)

# Mirrors the frontend dropzone's allow-list (lesson-form.tsx). The server is the real
# gate: the extension decides the stored suffix and bounds the content type.
ALLOWED_EXTENSIONS: frozenset[str] = frozenset({
    # Documents
    ".pdf", ".doc", ".docx", ".dot", ".dotx", ".odt", ".rtf", ".txt", ".md",
    ".pages", ".epub", ".json", ".xml", ".log",
    # Spreadsheets
    ".xls", ".xlsx", ".ods", ".csv", ".tsv", ".numbers",
    # Presentations
    ".ppt", ".pptx", ".pps", ".ppsx", ".odp", ".key",
    # Images
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg", ".bmp", ".ico",
    ".tif", ".tiff", ".heic", ".heif",
    # Archives
    ".zip",
})



class AssetNotFoundError(NotFoundError):
    code = "ASSET_NOT_FOUND"
    message = "Asset not found"


class AssetValidationError(ValidationError):
    code = "ASSET_INVALID"


class AssetTooLargeError(ValidationError):
    # 422 (ValidationError's status). The frontend also caps size before sending; this is
    # the backstop for anything that reaches the endpoint directly.
    code = "ASSET_TOO_LARGE"


class LessonNotFoundError(NotFoundError):
    code = "LESSON_NOT_FOUND"
    message = "Lesson not found"


class _Meter:
    """Wraps the upload stream to measure it as it flows to storage: total size, sha256,
    and a hard size cap enforced mid-stream so an over-size body is cut off, not buffered.
    Read ``size``/``checksum`` after the stream has been fully consumed."""

    def __init__(self, source: AsyncIterator[bytes], *, max_bytes: int) -> None:
        self._source = source
        self._max_bytes = max_bytes
        self._hasher = hashlib.sha256()
        self.size = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._source:
            if not chunk:
                continue
            self.size += len(chunk)
            if self.size > self._max_bytes:
                raise AssetTooLargeError(
                    f"File exceeds the {self._max_bytes // (1024 * 1024)} MB limit"
                )
            self._hasher.update(chunk)
            yield chunk

    @property
    def checksum(self) -> str:
        return self._hasher.hexdigest()


def _clean_filename(filename: str) -> str:
    # Keep only the final path segment, so "../../etc/passwd" becomes "passwd". The name
    # is display-only (never part of storage_key), but a tidy name is still better.
    name = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or name in {".", ".."}:
        raise AssetValidationError("A filename is required")
    return name[:255]


def _checked_extension(filename: str) -> str:
    dot = filename.rfind(".")
    ext = filename[dot:].lower() if dot != -1 else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise AssetValidationError(
            f"Unsupported file type {ext or '(none)'!r}. Allowed: "
            + ", ".join(sorted(ALLOWED_EXTENSIONS))
        )
    return ext


def _resolve_content_type(content_type: str | None, filename: str) -> str:
    candidate = (content_type or "").split(";", 1)[0].strip().lower()
    if candidate and candidate != "application/octet-stream":
        return candidate[:255]
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


async def _visible_course(
    session: AsyncSession, course_id: uuid.UUID, *, viewer_id: uuid.UUID, role: str
) -> Course | None:
    course = await get_course_or_404(session, course_id)
    if course.status == CourseStatus.PUBLISHED or can_modify(
        course, current_user_id=viewer_id, role=role
    ):
        return course
    return None


async def _lesson_in_course(
    session: AsyncSession, lesson_id: uuid.UUID, course_id: uuid.UUID
) -> Lesson:
    lesson = await session.get(Lesson, lesson_id)
    if lesson is None or lesson.course_id != course_id:
        raise AssetValidationError("The lesson does not belong to this course")
    return lesson


async def create_asset(
    session: AsyncSession,
    storage: Storage,
    *,
    course_id: uuid.UUID,
    lesson_id: uuid.UUID | None,
    filename: str,
    content_type: str | None,
    source: AsyncIterator[bytes],
    uploaded_by: uuid.UUID,
    role: str,
    max_bytes: int,
) -> Asset:
    """Stream an upload to storage and record it. The course is locked and checked
    editable first; a failed write or insert removes the stored bytes so a partial upload
    never leaves an orphan."""
    course = await get_course_for_update(session, course_id)
    ensure_can_modify(course, current_user_id=uploaded_by, role=role, action="upload files to")
    ensure_content_editable(course)
    if lesson_id is not None:
        await _lesson_in_course(session, lesson_id, course_id)

    filename = _clean_filename(filename)
    ext = _checked_extension(filename)
    resolved_type = _resolve_content_type(content_type, filename)

    asset_id = uuid.uuid4()
    storage_key = f"{course_id}/{asset_id}{ext}"
    meter = _Meter(source, max_bytes=max_bytes)
    try:
        await storage.put(storage_key, meter.__aiter__())
    except BaseException:
        await _delete_quietly(storage, storage_key)
        raise
    if meter.size == 0:
        await _delete_quietly(storage, storage_key)
        raise AssetValidationError("The file is empty")

    asset = Asset(
        id=asset_id,
        course_id=course_id,
        lesson_id=lesson_id,
        filename=filename,
        content_type=resolved_type,
        size_bytes=meter.size,
        storage_key=storage_key,
        checksum_sha256=meter.checksum,
        uploaded_by=uploaded_by,
    )
    session.add(asset)
    try:
        await session.flush()
    except BaseException:
        await _delete_quietly(storage, storage_key)
        raise
    return asset


async def get_asset(
    session: AsyncSession, asset_id: uuid.UUID, *, viewer_id: uuid.UUID, role: str
) -> Asset:
    asset = await session.get(Asset, asset_id)
    if asset is None:
        raise AssetNotFoundError()
    if await _visible_course(session, asset.course_id, viewer_id=viewer_id, role=role) is None:
        raise AssetNotFoundError()
    return asset


def open_asset(storage: Storage, asset: Asset) -> AsyncIterator[bytes]:
    """Byte stream for a download. Call ``get_asset`` first for the access check."""
    return storage.get(asset.storage_key)


async def list_lesson_assets(
    session: AsyncSession, lesson_id: uuid.UUID, *, viewer_id: uuid.UUID, role: str
) -> list[Asset]:
    lesson = await session.get(Lesson, lesson_id)
    if lesson is None:
        raise LessonNotFoundError()
    if await _visible_course(session, lesson.course_id, viewer_id=viewer_id, role=role) is None:
        raise LessonNotFoundError()
    rows = await session.execute(
        select(Asset).where(Asset.lesson_id == lesson_id).order_by(Asset.created_at)
    )
    return list(rows.scalars())


async def delete_asset(
    session: AsyncSession, asset_id: uuid.UUID, *, current_user_id: uuid.UUID, role: str
) -> str:
    """Remove the row (course locked + editable + owner). Returns the storage key; the
    caller deletes the bytes *after* the transaction commits (a background task), so a
    rollback can never leave a row pointing at missing bytes."""
    asset = await session.get(Asset, asset_id)
    if asset is None:
        raise AssetNotFoundError()
    course = await get_course_for_update(session, asset.course_id)
    ensure_can_modify(
        course, current_user_id=current_user_id, role=role, action="delete files from"
    )
    ensure_content_editable(course)
    storage_key = asset.storage_key
    await session.delete(asset)
    await session.flush()
    return storage_key


async def _delete_quietly(storage: Storage, key: str) -> None:
    try:
        await storage.delete(key)
    except StorageError as exc:
        logger.warning("asset_orphan_cleanup_failed", storage_key=key, detail=str(exc))
