"""Success envelope + cursor pagination. Every success response uses ``success()``;
never a bare dict or list.
"""

import base64
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from src.core.logging import current_request_id


class Meta(BaseModel):
    request_id: str | None = None
    timestamp: str


class Envelope[DataT](BaseModel):
    data: DataT
    meta: Meta


def success[DataT](data: DataT) -> Envelope[DataT]:
    meta = Meta(request_id=current_request_id(), timestamp=datetime.now(tz=UTC).isoformat())
    return Envelope(data=data, meta=meta)


class CursorPage[ItemT](BaseModel):
    items: list[ItemT]
    next_cursor: str | None = None
    has_more: bool = False


def encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: str) -> dict[str, Any]:
    raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
    payload: dict[str, Any] = json.loads(raw)
    return payload
