"""Exception hierarchy for MunshiJi.

Anything raised deliberately by application code inherits from :class:`MunshiJiError`, which
carries an HTTP status so the API layer can translate without a giant ``isinstance`` ladder.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ApprovalRequiredError",
    "InvalidStateTransitionError",
    "MunshiJiError",
    "NotFoundError",
    "ProviderUnavailableError",
    "RateLimitedError",
    "ToolExecutionError",
    "ToolNotFoundError",
    "UnauthorizedError",
    "ValidationError",
]


class MunshiJiError(Exception):
    """Base class for expected application errors."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = context

    def to_payload(self) -> dict[str, Any]:
        """Serialisable body for an API error response."""
        return {"error": {"code": self.code, "message": self.message, "context": self.context}}


class NotFoundError(MunshiJiError):
    status_code = 404
    code = "not_found"


class UnauthorizedError(MunshiJiError):
    status_code = 401
    code = "unauthorized"


class ValidationError(MunshiJiError):
    status_code = 422
    code = "validation_error"


class ProviderUnavailableError(MunshiJiError):
    """A live vendor provider could not serve the request; the caller may fall back to local."""

    status_code = 503
    code = "provider_unavailable"


class ToolNotFoundError(MunshiJiError):
    status_code = 404
    code = "tool_not_found"


class ToolExecutionError(MunshiJiError):
    status_code = 500
    code = "tool_execution_failed"


class ApprovalRequiredError(MunshiJiError):
    """A write tool was invoked without an approved :class:`ActionRequest` behind it."""

    status_code = 409
    code = "approval_required"


class RateLimitedError(MunshiJiError):
    status_code = 429
    code = "rate_limited"


class InvalidStateTransitionError(MunshiJiError):
    status_code = 409
    code = "invalid_state_transition"
