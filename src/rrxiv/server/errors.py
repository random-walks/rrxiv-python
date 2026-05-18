"""RFC 9457 problem-details exception handlers for the reference server.

Per ``spec/0007-api.md``, error responses use ``application/problem+json``
with the standard fields (``type``, ``title``, ``status``, ``detail``).
This module defines a single :class:`ProblemError` that all
domain code raises and one handler that renders it consistently.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

PROBLEM_BASE = "https://rrxiv.com/errors/"


class ProblemError(Exception):
    """A typed problem-details exception.

    Domain services and dependencies raise this; the handler attached
    in :func:`install_exception_handlers` converts it to the right
    JSON response.
    """

    def __init__(
        self,
        *,
        status: int,
        title: str,
        detail: str,
        type_slug: str | None = None,
        extra: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.title = title
        self.detail = detail
        self.type = PROBLEM_BASE + (type_slug or _slug_from_title(title))
        self.extra = extra or {}
        self.headers = headers or {}


def _slug_from_title(title: str) -> str:
    return title.lower().replace(" ", "-")


# Convenience constructors so route code stays readable.
def bad_request(detail: str, *, extra: dict[str, Any] | None = None) -> ProblemError:
    return ProblemError(
        status=400, title="Bad Request", detail=detail, extra=extra
    )


def unauthorized(detail: str = "missing or invalid token") -> ProblemError:
    return ProblemError(status=401, title="Unauthorized", detail=detail)


def forbidden(detail: str) -> ProblemError:
    return ProblemError(status=403, title="Forbidden", detail=detail)


def not_found(detail: str) -> ProblemError:
    return ProblemError(status=404, title="Not Found", detail=detail)


def conflict(detail: str) -> ProblemError:
    return ProblemError(
        status=409, title="Idempotency Key Conflict", detail=detail
    )


def validation_error(
    detail: str, *, extra: dict[str, Any] | None = None
) -> ProblemError:
    return ProblemError(
        status=422, title="Validation Error", detail=detail, extra=extra
    )


def rate_limited(retry_after: int) -> ProblemError:
    return ProblemError(
        status=429,
        title="Rate Limited",
        detail=f"retry after {retry_after} seconds",
        headers={"Retry-After": str(retry_after)},
    )


def install_exception_handlers(app: FastAPI) -> None:
    """Attach handlers for ProblemError and pydantic validation."""

    @app.exception_handler(ProblemError)
    async def _handle_problem(_: Request, exc: ProblemError) -> JSONResponse:
        body: dict[str, Any] = {
            "type": exc.type,
            "title": exc.title,
            "status": exc.status,
            "detail": exc.detail,
        }
        body.update(exc.extra)
        return JSONResponse(
            status_code=exc.status,
            content=body,
            media_type="application/problem+json",
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Surface FastAPI's default error breakdown under "errors" so
        # clients can pinpoint the field. We keep the schema close to
        # FastAPI's so OpenAPI consumers aren't surprised.
        body = {
            "type": PROBLEM_BASE + "validation-error",
            "title": "Validation Error",
            "status": 422,
            "detail": "request body or params failed validation",
            "errors": exc.errors(),
        }
        return JSONResponse(
            status_code=422,
            content=body,
            media_type="application/problem+json",
        )
