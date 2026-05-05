"""HTTP client error hierarchy.

All errors derive from :class:`RrxivError`. Specific subclasses map to
the standard status codes used in the API per
``rrxiv/spec/0007-api.md``.
"""

from __future__ import annotations

from typing import Any

import httpx


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


def raise_for_status(response: httpx.Response) -> None:
    """Map HTTP status code to a typed :class:`RrxivError`.

    No-op on 2xx. Used by :class:`rrxiv.client.RrxivClient`,
    :class:`rrxiv.client.AsyncRrxivClient`, and the
    :mod:`rrxiv.auth` flow helpers so they share one wire-error
    interpretation.
    """
    if 200 <= response.status_code < 300:
        return

    problem: dict[str, Any] = {}
    if response.headers.get("content-type", "").startswith("application/problem+json"):
        try:
            problem = response.json()
        except ValueError:
            pass
    detail = problem.get("detail") or response.reason_phrase or "request failed"
    title = problem.get("title")
    msg = f"{title}: {detail}" if title else detail

    cls: type[RrxivError]
    if response.status_code == 400:
        cls = BadRequestError
    elif response.status_code == 401:
        cls = UnauthorizedError
    elif response.status_code == 403:
        cls = ForbiddenError
    elif response.status_code == 404:
        cls = NotFoundError
    elif response.status_code == 409:
        cls = IdempotencyKeyConflictError
    elif response.status_code == 422:
        cls = ValidationError
    elif response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        retry_after_seconds = (
            int(retry_after) if retry_after and retry_after.isdigit() else None
        )
        raise RateLimitedError(
            msg,
            retry_after_seconds=retry_after_seconds,
            problem=problem,
        )
    elif 500 <= response.status_code < 600:
        cls = ServerError
    else:
        cls = RrxivError
    raise cls(msg, status_code=response.status_code, problem=problem)
