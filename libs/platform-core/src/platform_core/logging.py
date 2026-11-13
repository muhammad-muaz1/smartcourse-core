"""structlog setup + request-scoped context, in one file because they're two sides of
the same concern: request_id must reach every log line without being passed manually,
and that only works if the same contextvars machinery configures both.
"""

import logging
import sys
from collections.abc import Mapping
from typing import Any

import structlog

from platform_core.telemetry import current_trace_ids

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


def _service_name_adder(service_name: str) -> Any:
    def add_service_name(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        event_dict.setdefault("service.name", service_name)
        return event_dict

    return add_service_name


def _add_trace_ids(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """trace_id/span_id on every line logged inside a span, so a log line found in one
    tool opens the right trace in Jaeger — never passed manually (CLAUDE.md §11)."""
    ids = current_trace_ids()
    if ids is not None:
        event_dict.setdefault("trace_id", ids[0])
        event_dict.setdefault("span_id", ids[1])
    return event_dict


def configure_logging(*, service_name: str, level: str = "INFO", json_logs: bool = True) -> None:
    numeric_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        # With several services writing to one log stream, a line that doesn't say
        # which service wrote it is unattributable.
        _service_name_adder(service_name),
        _add_trace_ids,
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
