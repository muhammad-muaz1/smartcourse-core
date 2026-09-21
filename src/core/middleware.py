"""Every middleware we actually use, in one file. Registered in app.py in this exact
order: RequestContextMiddleware first (so request_id exists before anything else runs),
then CORSMiddleware (starlette's own, added directly in app.py — nothing to define here).

Rate limiting, idempotency keys, body-size limits, security headers and trusted hosts
are real needs, added when the endpoint that needs them is built, not before — see
CLAUDE.md §6.
"""

import time
import uuid

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.core.logging import bind_request_id, clear_request_context

REQUEST_ID_HEADER = "X-Request-ID"

logger = structlog.get_logger("smartcourse.access")


class RequestContextMiddleware:
    """Accepts or mints X-Request-ID, binds it into a contextvar for every log line
    in this request, and logs one access line with method/path/status/duration.

    Pure ASGI (not BaseHTTPMiddleware) to avoid its known issues with streaming
    responses and background tasks.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = Headers(scope=scope).get(REQUEST_ID_HEADER) or _new_request_id()
        bind_request_id(request_id)

        start = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            await logger.ainfo(
                "http_request",
                method=scope.get("method", ""),
                path=scope.get("path", ""),
                status_code=status_code,
                duration_ms=duration_ms,
            )
            clear_request_context()


def _new_request_id() -> str:
    return str(uuid.uuid4())
