"""Asset upload/read/list/delete. Requires Docker (testcontainers Postgres).

Service logic runs against an in-memory fake Storage; the LocalFilesystemStorage backend
has its own round-trip test against tmp_path. The MinioStorage backend is covered by a
separate, skipped-by-default integration test (needs a running MinIO + aioboto3).
"""

import hashlib
import uuid
from collections.abc import AsyncIterator

import pytest

from platform_core.exceptions import ForbiddenError
from src.db import postgres
from src.modules.assets import service
from src.modules.assets.service import (
    AssetNotFoundError,
    AssetTooLargeError,
    AssetValidationError,
)
from src.modules.assets.storage import (
    LocalFilesystemStorage,
    ObjectNotFoundError,
    Storage,
    StorageError,
)
from src.modules.courses.models import CourseStatus
from src.modules.courses.service import courses as courses_service
from tests.courses.helpers import INSTRUCTOR, make_course


class FakeStorage:
    """In-memory Storage for the service tests: records what was written and can replay
    it, so a test can assert both the metadata and the stored bytes."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, chunks: AsyncIterator[bytes]) -> None:
        buffer = bytearray()
        async for chunk in chunks:
            buffer += chunk
        self.objects[key] = bytes(buffer)

    def get(self, key: str) -> AsyncIterator[bytes]:
        async def _stream() -> AsyncIterator[bytes]:
            if key not in self.objects:
                raise ObjectNotFoundError(key)
            yield self.objects[key]

        return _stream()

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)


async def _source(data: bytes, *, chunk: int = 7) -> AsyncIterator[bytes]:
    for i in range(0, len(data), chunk):
        yield data[i : i + chunk]


async def _drain(stream: AsyncIterator[bytes]) -> bytes:
    out = bytearray()
    async for chunk in stream:
        out += chunk
    return bytes(out)


async def _published_course(session, owner: uuid.UUID):
    course = await make_course(session, owner, modules=1, lessons_each=1)
    course.status = CourseStatus.PUBLISHED
    await session.flush()
    return course


async def _lesson_id(session, course_id: uuid.UUID) -> uuid.UUID:
    from src.modules.courses.service import structure

    tree = await structure.module_tree(session, course_id)
    return tree[0].lessons[0].id


class TestUpload:
    async def test_uploads_record_size_checksum_and_bytes(self, _db: None) -> None:
        owner = uuid.uuid4()
        storage = FakeStorage()
        data = b"%PDF-1.4 fake pdf bytes" * 100
        async with postgres.database.session_scope() as session:
            course = await make_course(session, owner, modules=1, lessons_each=1)
            lesson_id = await _lesson_id(session, course.id)

            asset = await service.create_asset(
                session,
                storage,
                course_id=course.id,
                lesson_id=lesson_id,
                filename="syllabus.pdf",
                content_type="application/pdf",
                source=_source(data),
                uploaded_by=owner,
                role=INSTRUCTOR,
                max_bytes=10_000_000,
            )

            assert asset.size_bytes == len(data)
            assert asset.checksum_sha256 == hashlib.sha256(data).hexdigest()
            assert asset.content_type == "application/pdf"
            assert asset.filename == "syllabus.pdf"
            assert asset.storage_key == f"{course.id}/{asset.id}.pdf"
            assert storage.objects[asset.storage_key] == data

            # Round-trips through the read path.
            fetched = await service.get_asset(
                session, asset.id, viewer_id=owner, role=INSTRUCTOR
            )
            assert await _drain(service.open_asset(storage, fetched)) == data
            listed = await service.list_lesson_assets(
                session, lesson_id, viewer_id=owner, role=INSTRUCTOR
            )
            assert [a.id for a in listed] == [asset.id]

    async def test_content_type_falls_back_to_the_extension(self, _db: None) -> None:
        owner = uuid.uuid4()
        storage: Storage = FakeStorage()
        async with postgres.database.session_scope() as session:
            course = await make_course(session, owner)
            asset = await service.create_asset(
                session,
                storage,
                course_id=course.id,
                lesson_id=None,
                filename="notes.csv",
                content_type="application/octet-stream",  # uninformative → guess from name
                source=_source(b"a,b,c\n1,2,3\n"),
                uploaded_by=owner,
                role=INSTRUCTOR,
                max_bytes=10_000,
            )
            assert asset.content_type == "text/csv"

    async def test_rejects_unsupported_extension(self, _db: None) -> None:
        owner = uuid.uuid4()
        storage = FakeStorage()
        async with postgres.database.session_scope() as session:
            course = await make_course(session, owner)
            with pytest.raises(AssetValidationError):
                await service.create_asset(
                    session,
                    storage,
                    course_id=course.id,
                    lesson_id=None,
                    filename="malware.exe",
                    content_type="application/octet-stream",
                    source=_source(b"MZ..."),
                    uploaded_by=owner,
                    role=INSTRUCTOR,
                    max_bytes=10_000,
                )
            assert storage.objects == {}  # nothing stored

    async def test_rejects_and_cleans_up_an_oversize_upload(self, _db: None) -> None:
        owner = uuid.uuid4()
        storage = FakeStorage()
        async with postgres.database.session_scope() as session:
            course = await make_course(session, owner)
            with pytest.raises(AssetTooLargeError):
                await service.create_asset(
                    session,
                    storage,
                    course_id=course.id,
                    lesson_id=None,
                    filename="big.pdf",
                    content_type="application/pdf",
                    source=_source(b"x" * 5_000),
                    uploaded_by=owner,
                    role=INSTRUCTOR,
                    max_bytes=1_000,
                )
            assert storage.objects == {}  # partial object removed

    async def test_rejects_empty_file(self, _db: None) -> None:
        owner = uuid.uuid4()
        storage = FakeStorage()
        async with postgres.database.session_scope() as session:
            course = await make_course(session, owner)
            with pytest.raises(AssetValidationError):
                await service.create_asset(
                    session,
                    storage,
                    course_id=course.id,
                    lesson_id=None,
                    filename="empty.txt",
                    content_type="text/plain",
                    source=_source(b""),
                    uploaded_by=owner,
                    role=INSTRUCTOR,
                    max_bytes=1_000,
                )
            assert storage.objects == {}

    async def test_lesson_must_belong_to_course(self, _db: None) -> None:
        owner = uuid.uuid4()
        storage = FakeStorage()
        async with postgres.database.session_scope() as session:
            course = await make_course(session, owner)
            other = await make_course(session, owner, modules=1, lessons_each=1)
            foreign_lesson = await _lesson_id(session, other.id)
            with pytest.raises(AssetValidationError):
                await service.create_asset(
                    session,
                    storage,
                    course_id=course.id,
                    lesson_id=foreign_lesson,
                    filename="a.pdf",
                    content_type="application/pdf",
                    source=_source(b"data"),
                    uploaded_by=owner,
                    role=INSTRUCTOR,
                    max_bytes=10_000,
                )

    async def test_non_owner_forbidden_admin_allowed(self, _db: None) -> None:
        owner = uuid.uuid4()
        storage = FakeStorage()
        async with postgres.database.session_scope() as session:
            course = await make_course(session, owner)
            with pytest.raises(ForbiddenError):
                await service.create_asset(
                    session,
                    storage,
                    course_id=course.id,
                    lesson_id=None,
                    filename="a.pdf",
                    content_type="application/pdf",
                    source=_source(b"data"),
                    uploaded_by=uuid.uuid4(),
                    role=INSTRUCTOR,
                    max_bytes=10_000,
                )
            asset = await service.create_asset(
                session,
                storage,
                course_id=course.id,
                lesson_id=None,
                filename="a.pdf",
                content_type="application/pdf",
                source=_source(b"data"),
                uploaded_by=uuid.uuid4(),
                role="admin",
                max_bytes=10_000,
            )
            assert asset.id is not None

    async def test_upload_blocked_once_course_not_editable(self, _db: None) -> None:
        owner = uuid.uuid4()
        storage = FakeStorage()
        async with postgres.database.session_scope() as session:
            course = await _published_course(session, owner)
            with pytest.raises(courses_service.CourseNotEditableError):
                await service.create_asset(
                    session,
                    storage,
                    course_id=course.id,
                    lesson_id=None,
                    filename="a.pdf",
                    content_type="application/pdf",
                    source=_source(b"data"),
                    uploaded_by=owner,
                    role=INSTRUCTOR,
                    max_bytes=10_000,
                )


class TestVisibilityAndDelete:
    async def test_draft_asset_hidden_from_others_published_visible(self, _db: None) -> None:
        owner = uuid.uuid4()
        storage = FakeStorage()
        async with postgres.database.session_scope() as session:
            course = await make_course(session, owner)
            asset = await service.create_asset(
                session,
                storage,
                course_id=course.id,
                lesson_id=None,
                filename="a.pdf",
                content_type="application/pdf",
                source=_source(b"data"),
                uploaded_by=owner,
                role=INSTRUCTOR,
                max_bytes=10_000,
            )
            # A stranger can't see an asset of a draft course.
            with pytest.raises(AssetNotFoundError):
                await service.get_asset(
                    session, asset.id, viewer_id=uuid.uuid4(), role="student"
                )
            # Once published, anyone can.
            course.status = CourseStatus.PUBLISHED
            await session.flush()
            seen = await service.get_asset(
                session, asset.id, viewer_id=uuid.uuid4(), role="student"
            )
            assert seen.id == asset.id

    async def test_delete_removes_row_and_returns_key(self, _db: None) -> None:
        owner = uuid.uuid4()
        storage = FakeStorage()
        async with postgres.database.session_scope() as session:
            course = await make_course(session, owner)
            asset = await service.create_asset(
                session,
                storage,
                course_id=course.id,
                lesson_id=None,
                filename="a.pdf",
                content_type="application/pdf",
                source=_source(b"data"),
                uploaded_by=owner,
                role=INSTRUCTOR,
                max_bytes=10_000,
            )
            key = await service.delete_asset(
                session, asset.id, current_user_id=owner, role=INSTRUCTOR
            )
            assert key == asset.storage_key
            with pytest.raises(AssetNotFoundError):
                await service.get_asset(session, asset.id, viewer_id=owner, role=INSTRUCTOR)


class TestLocalFilesystemStorage:
    async def test_round_trip_and_delete(self, tmp_path) -> None:
        storage = LocalFilesystemStorage(tmp_path)
        key = "course-x/asset-y.pdf"
        await storage.put(key, _source(b"hello world", chunk=3))
        assert await _drain(storage.get(key)) == b"hello world"
        await storage.delete(key)
        with pytest.raises(ObjectNotFoundError):
            await _drain(storage.get(key))

    async def test_rejects_traversal_keys(self, tmp_path) -> None:
        storage = LocalFilesystemStorage(tmp_path)
        with pytest.raises(StorageError):
            await storage.put("../escape.pdf", _source(b"x"))
        with pytest.raises(StorageError):
            await storage.put("/abs/escape.pdf", _source(b"x"))
