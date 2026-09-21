"""AppError and its subclasses. Domain/service code raises these and never imports
FastAPI — the HTTP mapping lives entirely in core/handlers.py.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FieldError:
    field: str
    message: str


class AppError(Exception):
    code: str = "INTERNAL_ERROR"
    message: str = "Application error"

    def __init__(
        self, message: str | None = None, *, details: list[FieldError] | None = None
    ) -> None:
        super().__init__(message or self.message)
        self.message = message or self.message
        self.details = details or []


class NotFoundError(AppError):
    code = "NOT_FOUND"
    message = "Resource not found"


class ConflictError(AppError):
    code = "CONFLICT"
    message = "Conflict with current state"


class ValidationError(AppError):
    code = "VALIDATION_ERROR"
    message = "Validation failed"


class UnauthorizedError(AppError):
    code = "UNAUTHORIZED"
    message = "Authentication required"


class ForbiddenError(AppError):
    code = "FORBIDDEN"
    message = "Not allowed to perform this action"


class BusinessRuleError(AppError):
    code = "BUSINESS_RULE_VIOLATION"
    message = "Business rule violation"


class RateLimitedError(AppError):
    """Not in CLAUDE.md's original six — added for the auth module's login/register
    throttling (docs/adr/0008), a real, immediate caller, not a speculative one. 429 is
    a distinct HTTP semantic none of the other six correctly represent."""

    code = "RATE_LIMITED"
    message = "Too many requests"
