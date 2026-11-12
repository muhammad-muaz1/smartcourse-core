"""Exception -> HTTP response, registered once. No route ever builds an error body by
hand.
"""

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from platform_core.exceptions import (
    AppError,
    BusinessRuleError,
    ConflictError,
    FieldError,
    ForbiddenError,
    NotFoundError,
    RateLimitedError,
    UnauthorizedError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
    ValidationError,
)
from platform_core.logging import current_request_id

logger = structlog.get_logger(__name__)

# Keyed by exception class, resolved along the MRO, so a service can raise a subclass
# with its own stable code (``class AlreadyEnrolledError(ConflictError): code =
# "ALREADY_ENROLLED"``) and still get its parent's HTTP status.
_STATUS_BY_TYPE: dict[type, int] = {
    NotFoundError: status.HTTP_404_NOT_FOUND,
    ConflictError: status.HTTP_409_CONFLICT,
    ValidationError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    UnauthorizedError: status.HTTP_401_UNAUTHORIZED,
    ForbiddenError: status.HTTP_403_FORBIDDEN,
    BusinessRuleError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    RateLimitedError: status.HTTP_429_TOO_MANY_REQUESTS,
    UpstreamUnavailableError: status.HTTP_502_BAD_GATEWAY,
    UpstreamTimeoutError: status.HTTP_504_GATEWAY_TIMEOUT,
}


def _status_for(exc: AppError) -> int:
    for cls in type(exc).__mro__:
        http_status = _STATUS_BY_TYPE.get(cls)
        if http_status is not None:
            return http_status
    return status.HTTP_400_BAD_REQUEST


def error_body(
    *, code: str, message: str, details: list[FieldError] | None = None
) -> dict[str, object]:
    """The one error shape. Exception handlers use it, and so does middleware that has
    to answer before a route runs (idempotency) — nothing else builds an error body."""
    return {
        "code": code,
        "message": message,
        "details": [{"field": d.field, "message": d.message} for d in (details or [])],
        "request_id": current_request_id(),
    }


async def _handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
    http_status = _status_for(exc)
    if http_status >= status.HTTP_500_INTERNAL_SERVER_ERROR:
        await logger.aerror("unhandled_app_error", code=exc.code, message=exc.message)
    return JSONResponse(
        status_code=http_status,
        content=error_body(code=exc.code, message=exc.message, details=exc.details),
    )


async def _handle_request_validation_error(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    details = [
        FieldError(field=".".join(str(p) for p in err["loc"]), message=err["msg"])
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=error_body(code="VALIDATION_ERROR", message="Validation failed", details=details),
    )


async def _handle_http_exception(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = "NOT_FOUND" if exc.status_code == status.HTTP_404_NOT_FOUND else "HTTP_ERROR"
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(code=code, message=str(exc.detail)),
    )


async def _handle_unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
    await logger.aerror("unhandled_exception", exc_info=exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=error_body(code="INTERNAL_ERROR", message="An unexpected error occurred."),
    )


def register_exception_handlers(app: FastAPI) -> None:
    # FastAPI's documented pattern is a handler typed to the specific exception
    # subclass, but its own stubs only accept Callable[[Request, Exception], ...] —
    # a known stub/runtime mismatch, not a real type error.
    app.add_exception_handler(AppError, _handle_app_error)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _handle_request_validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _handle_unexpected_error)
