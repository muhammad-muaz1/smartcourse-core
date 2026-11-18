"""HTTP layer for assets. Validate, call service, return. No business logic here.

Upload is a raw-body stream, not multipart: the browser sends the file as the request
body with ``?course_id=&filename=&lesson_id=`` in the query and the file's MIME in
``Content-Type``. One file per request (the client loops over attachments). This keeps
the service free of a multipart dependency and lets the body stream straight to storage
instead of being buffered.
"""

import uuid
from functools import lru_cache
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from platform_core.response import Envelope, success
from src.config import Settings, get_settings
from src.core.dependencies import CurrentUser, get_current_user, require_roles
from src.db.postgres import get_session
from src.modules.assets import service
from src.modules.assets.schemas import AssetPublic
from src.modules.assets.storage import Storage, build_storage

router = APIRouter(tags=["assets"])

# Only staff upload or delete; who may *read* depends on the course's status (service.py).
_require_uploader = require_roles("instructor", "admin")


@lru_cache(maxsize=1)
def _shared_storage() -> Storage:
    # Stateless and cheap; one per process. lru_cache makes it a singleton without a
    # module-level global that would build at import time.
    return build_storage(get_settings().storage)


def get_storage() -> Storage:
    return _shared_storage()


def _public(asset: object) -> AssetPublic:
    return AssetPublic.model_validate(asset, from_attributes=True)


@router.post("/assets", status_code=status.HTTP_201_CREATED, response_model=Envelope[AssetPublic])
async def upload_asset_route(
    request: Request,
    course_id: uuid.UUID = Query(...),
    filename: str = Query(..., min_length=1, max_length=255),
    lesson_id: uuid.UUID | None = Query(default=None),
    current_user: CurrentUser = Depends(_require_uploader),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    storage: Storage = Depends(get_storage),
) -> Envelope[AssetPublic]:
    asset = await service.create_asset(
        session,
        storage,
        course_id=course_id,
        lesson_id=lesson_id,
        filename=filename,
        content_type=request.headers.get("content-type"),
        source=request.stream(),
        uploaded_by=current_user.id,
        role=current_user.role,
        max_bytes=settings.storage.max_bytes,
    )
    return success(_public(asset))


@router.get("/assets/{asset_id}", response_model=Envelope[AssetPublic])
async def get_asset_route(
    asset_id: uuid.UUID,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Envelope[AssetPublic]:
    asset = await service.get_asset(
        session, asset_id, viewer_id=current_user.id, role=current_user.role
    )
    return success(_public(asset))


@router.get("/assets/{asset_id}/content")
async def download_asset_route(
    asset_id: uuid.UUID,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    storage: Storage = Depends(get_storage),
) -> StreamingResponse:
    asset = await service.get_asset(
        session, asset_id, viewer_id=current_user.id, role=current_user.role
    )
    # The session is closed once this handler returns; the stream reads from storage, not
    # the database, so streaming after close is safe.
    return StreamingResponse(
        service.open_asset(storage, asset),
        media_type=asset.content_type,
        headers={
            "Content-Length": str(asset.size_bytes),
            "Content-Disposition": _content_disposition(asset.filename),
            # The bytes are user-uploaded: never let a browser sniff and execute them.
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/lessons/{lesson_id}/assets", response_model=Envelope[list[AssetPublic]])
async def list_lesson_assets_route(
    lesson_id: uuid.UUID,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Envelope[list[AssetPublic]]:
    assets = await service.list_lesson_assets(
        session, lesson_id, viewer_id=current_user.id, role=current_user.role
    )
    return success([_public(asset) for asset in assets])


@router.delete("/assets/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_asset_route(
    asset_id: uuid.UUID,
    current_user: CurrentUser = Depends(_require_uploader),
    session: AsyncSession = Depends(get_session),
    storage: Storage = Depends(get_storage),
) -> Response:
    storage_key = await service.delete_asset(
        session, asset_id, current_user_id=current_user.id, role=current_user.role
    )
    # Delete the bytes after the response is sent — i.e. after get_session has committed
    # the row removal. A rollback therefore never strands a row without its file.
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        background=BackgroundTask(storage.delete, storage_key),
    )


def _content_disposition(filename: str) -> str:
    """RFC 5987: an ASCII fallback plus a UTF-8 copy, so a name with non-ASCII or quotes
    can't break the header."""
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace('"', "")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
