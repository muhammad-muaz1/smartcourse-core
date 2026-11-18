"""Where asset bytes live, behind a three-method protocol.

The whole point of this file is the seam: ``Storage`` is an interface, and the rest of
the service (service.py, routes.py) depends only on it. Two backends implement it:
``MinioStorage`` (S3-compatible object storage — MinIO self-hosted, or any S3 endpoint),
and ``LocalFilesystemStorage`` (files under a directory; the default, so tests and a bare
checkout need nothing running). Switching is one config value; no caller changes.

Keys are opaque, server-generated paths (``<course_id>/<asset_id><ext>``, built from
UUIDs in service.py), never user input — so a backend can map a key straight onto a
filesystem path or an S3 object key. ``LocalFilesystemStorage`` still refuses an absolute
or ``..`` key as defence in depth.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol

import anyio
import structlog

if TYPE_CHECKING:
    from src.config import AssetStorageSettings

logger = structlog.get_logger(__name__)

# Bytes moved per read/write. 1 MiB keeps memory flat for a 50 MB upload or download.
CHUNK_SIZE = 1024 * 1024


class StorageError(Exception):
    """A storage backend failed to read, write, or delete an object."""


class ObjectNotFoundError(StorageError):
    """No object exists at the given key (the metadata row and the bytes disagree)."""


class Storage(Protocol):
    """The contract every backend implements. ``put`` consumes an async byte stream so
    an upload never has to be held whole in memory; ``get`` produces one so a download
    streams back the same way."""

    async def put(self, key: str, chunks: AsyncIterator[bytes]) -> None: ...

    def get(self, key: str) -> AsyncIterator[bytes]: ...

    async def delete(self, key: str) -> None: ...


class LocalFilesystemStorage:
    """Stores each object as a file under ``root``. Single-box / local-dev backend: it
    has no replication and no signed URLs, which is exactly why it is meant to be
    swapped for object storage in a real deployment."""

    def __init__(self, root: str | anyio.Path) -> None:
        self._root = anyio.Path(root)

    def _resolve(self, key: str) -> anyio.Path:
        pure = PurePosixPath(key)
        if pure.is_absolute() or ".." in pure.parts:
            raise StorageError(f"unsafe storage key: {key!r}")
        return self._root.joinpath(*pure.parts)

    async def put(self, key: str, chunks: AsyncIterator[bytes]) -> None:
        path = self._resolve(key)
        await path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with await anyio.open_file(path, "wb") as handle:
                async for chunk in chunks:
                    await handle.write(chunk)
        except OSError as exc:
            # Don't leave a half-written file behind for the metadata row to point at.
            with anyio.CancelScope(shield=True):
                await self._unlink_quietly(path)
            raise StorageError(f"writing {key!r}: {exc}") from exc

    def get(self, key: str) -> AsyncIterator[bytes]:
        # A plain method returning an async generator: callers get the iterator without
        # an extra await, and the open happens when iteration starts.
        return self._stream(key)

    async def _stream(self, key: str) -> AsyncIterator[bytes]:
        path = self._resolve(key)
        try:
            handle = await anyio.open_file(path, "rb")
        except FileNotFoundError as exc:
            raise ObjectNotFoundError(key) from exc
        async with handle:
            while chunk := await handle.read(CHUNK_SIZE):
                yield chunk

    async def delete(self, key: str) -> None:
        await self._unlink_quietly(self._resolve(key))

    @staticmethod
    async def _unlink_quietly(path: anyio.Path) -> None:
        with contextlib.suppress(FileNotFoundError):
            await path.unlink()


class MinioStorage:
    """S3-API object storage. The local/dev target is MinIO (self-hosted, ``make minio``),
    but because MinIO speaks the S3 API this same class runs unchanged against AWS S3 or
    any S3-compatible store — only the endpoint and credentials differ.

    aioboto3 is imported lazily so the dependency is needed only when this backend is
    selected (tests and the local-filesystem backend never import it). Uploads are capped
    at 50 MB upstream (``_Meter`` in service.py), so ``put`` buffers the body and sends it
    in one call; downloads stream chunk by chunk so a large file never loads whole.
    """

    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str = "us-east-1",
        secure: bool = False,
    ) -> None:
        import aioboto3  # noqa: PLC0415 - lazy: only when backend="minio"

        self._bucket = bucket
        self._session = aioboto3.Session()
        self._client_kwargs = {
            "service_name": "s3",
            "endpoint_url": endpoint_url,
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
            "region_name": region,
            "use_ssl": secure,
        }
        self._bucket_ready = False

    def _client(self) -> Any:
        return self._session.client(**self._client_kwargs)

    async def _ensure_bucket(self, s3: Any) -> None:
        if self._bucket_ready:
            return
        from botocore.exceptions import ClientError  # noqa: PLC0415

        try:
            await s3.head_bucket(Bucket=self._bucket)
        except ClientError:
            try:
                await s3.create_bucket(Bucket=self._bucket)
            except ClientError as exc:
                # A racing creator (or "already owned by you") is fine; anything else isn't.
                code = exc.response.get("Error", {}).get("Code", "")
                if code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                    raise StorageError(f"creating bucket {self._bucket!r}: {exc}") from exc
        self._bucket_ready = True

    async def put(self, key: str, chunks: AsyncIterator[bytes]) -> None:
        buffer = bytearray()
        async for chunk in chunks:
            buffer += chunk
        try:
            async with self._client() as s3:
                await self._ensure_bucket(s3)
                await s3.put_object(Bucket=self._bucket, Key=key, Body=bytes(buffer))
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalise any boto/transport error
            raise StorageError(f"writing {key!r}: {exc}") from exc

    def get(self, key: str) -> AsyncIterator[bytes]:
        return self._stream(key)

    async def _stream(self, key: str) -> AsyncIterator[bytes]:
        from botocore.exceptions import ClientError  # noqa: PLC0415

        async with self._client() as s3:
            try:
                response = await s3.get_object(Bucket=self._bucket, Key=key)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in {"NoSuchKey", "NoSuchBucket", "404"}:
                    raise ObjectNotFoundError(key) from exc
                raise StorageError(f"reading {key!r}: {exc}") from exc
            async with response["Body"] as body:
                async for chunk in body.iter_chunks(CHUNK_SIZE):
                    yield chunk

    async def delete(self, key: str) -> None:
        try:
            async with self._client() as s3:
                await s3.delete_object(Bucket=self._bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"deleting {key!r}: {exc}") from exc


def build_storage(settings: AssetStorageSettings) -> Storage:
    """Pick a backend from ``Settings.storage``. Add a backend here and in config.py's
    ``AssetStorageSettings``."""
    if settings.backend == "local":
        return LocalFilesystemStorage(settings.local_dir)
    if settings.backend == "minio":
        return MinioStorage(
            endpoint_url=settings.minio_endpoint_url,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            bucket=settings.minio_bucket,
            region=settings.minio_region,
            secure=settings.minio_secure,
        )
    raise StorageError(
        f"unsupported storage backend {settings.backend!r} — expected 'local' or 'minio'"
    )

# For large videos, prefer a presigned PUT so the browser uploads straight to MinIO/S3 and
# the bytes never pass through the gateway (which buffers whole bodies): add
# ``presigned_put_url(key)`` here and have routes.py hand the URL to the client.
