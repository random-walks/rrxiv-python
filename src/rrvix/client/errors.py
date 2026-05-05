"""HTTP client error hierarchy.

All errors derive from :class:`RrvixError`. Specific subclasses map to
the standard status codes used in the API per
``rrvix/spec/0007-api.md``.
"""

from __future__ import annotations

from typing import Any


class RrvixError(Exception):
    """Base for all rrvix HTTP client errors."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        problem: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.problem = problem or {}


class BadRequestError(RrvixError):
    """400 — malformed request body."""


class UnauthorizedError(RrvixError):
    """401 — missing or invalid auth token."""


class ForbiddenError(RrvixError):
    """403 — auth identity not permitted to perform the action."""


class NotFoundError(RrvixError):
    """404 — resource not found."""


class IdempotencyKeyConflictError(RrvixError):
    """409 — Idempotency-Key collision with a different body."""


class ValidationError(RrvixError):
    """422 — request rejected on validation grounds."""


class RateLimitedError(RrvixError):
    """429 — rate limit exceeded. Inspect ``retry_after_seconds``."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: int | None = None,
        problem: dict[str, Any] | None = None,
    ):
        super().__init__(message, status_code=429, problem=problem)
        self.retry_after_seconds = retry_after_seconds


class ServerError(RrvixError):
    """5xx — server bug or overload. May be transient."""
