"""Wire shapes for assets. The client builds the download URL from ``id``
(``/api/v1/assets/{id}/content``); it's fetched with the bearer token, not linked."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class AssetPublic(BaseModel):
    id: uuid.UUID
    course_id: uuid.UUID
    lesson_id: uuid.UUID | None
    filename: str
    content_type: str
    size_bytes: int
    checksum_sha256: str
    created_at: datetime
