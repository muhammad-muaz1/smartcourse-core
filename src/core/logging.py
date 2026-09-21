"""structlog setup + request-scoped context, in one file because they're two sides of
the same concern: request_id must reach every log line without being passed manually,
and that only works if the same contextvars machinery configures both.
"""

import logging
import sys
from collections.abc import Mapping
from typing import Any

import structlog

_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")

REQUEST_ID_KEY = "request_id"


def bind_request_id(request_id: str) -> None:
    structlog.contextvars.bind_contextvars(**{REQUEST_ID_KEY: request_id})


def current_request_id() -> str | None:
    value: Mapping[str, Any] = structlog.contextvars.get_contextvars()
    request_id = value.get(REQUEST_ID_KEY)
    return str(request_id) if request_id is not None else None


def clear_request_context() -> None:
    structlog.contextvars.clear_contextvars()


def configure_logging(*, level: str = "INFO", json_logs: bool = True) -> None:
    numeric_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        cache_logger_on_first_use=True,
    )

    renderer = structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(numeric_level)

    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = [handler]
        uvicorn_logger.propagate = False
