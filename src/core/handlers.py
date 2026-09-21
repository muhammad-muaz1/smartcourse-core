"""Exception -> HTTP response, registered once. No route ever builds an error body by
hand.
"""

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.core.exceptions import AppError, FieldError
from src.core.logging import current_request_id

logger = structlog.get_logger(__name__)

_STATUS_BY_CODE: dict[str, int] = {
    "NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "CONFLICT": status.HTTP_409_CONFLICT,
    "VALIDATION_ERROR": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "UNAUTHORIZED": status.HTTP_401_UNAUTHORIZED,
    "FORBIDDEN": status.HTTP_403_FORBIDDEN,
    "BUSINESS_RULE_VIOLATION": status.HTTP_422_UNPROCESSABLE_CONTENT,
}


def _body(*, code: str, message: str, details: list[FieldError] | None = None) -> dict[str, object]:
    return {
        "code": code,
        "message": message,
        "details": [{"field": d.field, "message": d.message} for d in (details or [])],
        "request_id": current_request_id(),
    }


async def _handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
    http_status = _STATUS_BY_CODE.get(exc.code, status.HTTP_400_BAD_REQUEST)
    if http_status >= status.HTTP_500_INTERNAL_SERVER_ERROR:
        await logger.aerror("unhandled_app_error", code=exc.code, message=exc.message)
    return JSONResponse(
        status_code=http_status,
        content=_body(code=exc.code, message=exc.message, details=exc.details),
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
        content=_body(code="VALIDATION_ERROR", message="Validation failed", details=details),
    )


async def _handle_http_exception(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = "NOT_FOUND" if exc.status_code == status.HTTP_404_NOT_FOUND else "HTTP_ERROR"
    return JSONResponse(
        status_code=exc.status_code,
        content=_body(code=code, message=str(exc.detail)),
    )


async def _handle_unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
    await logger.aerror("unhandled_exception", exc_info=exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_body(code="INTERNAL_ERROR", message="An unexpected error occurred."),
    )


def register_exception_handlers(app: FastAPI) -> None:
    # FastAPI's documented pattern is a handler typed to the specific exception
    # subclass, but its own stubs only accept Callable[[Request, Exception], ...] —
    # a known stub/runtime mismatch, not a real type error.
    app.add_exception_handler(AppError, _handle_app_error)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _handle_request_validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _handle_unexpected_error)
