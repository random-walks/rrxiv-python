"""HTTP client error hierarchy.

All errors derive from :class:`RrxivError`. Specific subclasses map to
the standard status codes used in the API per
``rrxiv/spec/0007-api.md``.
"""

from __future__ import annotations

from typing import Any


class RrxivError(Exception):
    """Base for all rrxiv HTTP client errors."""

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


class BadRequestError(RrxivError):
    """400 — malformed request body."""


class UnauthorizedError(RrxivError):
    """401 — missing or invalid auth token."""


class ForbiddenError(RrxivError):
    """403 — auth identity not permitted to perform the action."""


class NotFoundError(RrxivError):
    """404 — resource not found."""


class IdempotencyKeyConflictError(RrxivError):
    """409 — Idempotency-Key collision with a different body."""


class ValidationError(RrxivError):
    """422 — request rejected on validation grounds."""


class RateLimitedError(RrxivError):
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


class ServerError(RrxivError):
    """5xx — server bug or overload. May be transient."""
