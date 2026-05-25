"""Observability helpers — identity attachment, route tagging,
structured request logging.

Two surfaces:

1. **Sentry scope enrichment.** ``attach_identity_to_scope`` and
   ``tag`` set the current Sentry scope's ``user`` and ``tags`` so
   captured exceptions carry rrxiv-domain context (which ORCID
   submitted, which paper the 500 was on, what annotation type, etc).
   No-ops when sentry-sdk isn't installed or no DSN is configured —
   the helpers are import-cheap and call-cheap.

2. **Structured access log.** ``RequestLoggingMiddleware`` emits one
   JSON record per request to a dedicated ``rrxiv.access`` logger.
   Toggleable via ``RRXIV_LOG_FORMAT=json`` (production) vs ``plain``
   (dev). The middleware reads identity off ``request.state.authed``
   when ``deps.py`` has populated it; otherwise records ``auth_kind=
   none``.

Both are kept in this single module so the wiring decision is in one
place. The router-level ``tag(...)`` calls in handlers depend only on
sentry-sdk being optional — no FastAPI imports.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from rrxiv.server.store import (
    AgentIdentity,
    AnonymousIdentity,
    Identity,
    OrcidIdentity,
)

try:
    import sentry_sdk
except ImportError:  # pragma: no cover - extras gated
    sentry_sdk = None  # type: ignore[assignment]


__all__ = [
    "RequestLoggingMiddleware",
    "attach_identity_to_scope",
    "clear_scope_identity",
    "identity_descriptor",
    "tag",
]


_access_log = logging.getLogger("rrxiv.access")


def _configure_access_logger() -> None:
    """Wire ``rrxiv.access`` so the middleware's records actually
    surface. By default the logger inherits from root, which under
    uvicorn doesn't always have an INFO-level handler — we'd write
    JSON lines into the void. Idempotent; safe to re-run."""
    if _access_log.handlers:
        return
    _access_log.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _access_log.addHandler(handler)
    # Don't double-log through uvicorn's root handler.
    _access_log.propagate = False


_configure_access_logger()


def identity_descriptor(identity: Identity | None) -> tuple[str, str]:
    """Return ``(kind, display_id)`` for an identity.

    ``kind`` is one of ``orcid``, ``agent``, ``anonymous``, ``none``.
    ``display_id`` is the human-readable handle / ORCID iD / a
    ``anon:<challenge>`` synthetic id / ``-`` for none.
    """
    if identity is None:
        return ("none", "-")
    if isinstance(identity, OrcidIdentity):
        return ("orcid", identity.orcid_id)
    if isinstance(identity, AgentIdentity):
        return ("agent", identity.handle)
    if isinstance(identity, AnonymousIdentity):
        return ("anonymous", f"anon:{identity.challenge_id}")
    return ("unknown", "-")


def attach_identity_to_scope(identity: Identity | None) -> None:
    """Stamp the current Sentry scope with the authenticated identity.

    Called after auth resolution in ``deps.py``. Idempotent; safe to
    call on every request. No-op when sentry-sdk isn't installed.
    """
    if sentry_sdk is None or identity is None:
        return
    kind, display_id = identity_descriptor(identity)
    # set_user with id+segment is the recommended shape; segment lets
    # us filter "all errors from agent identities" in one click.
    sentry_sdk.set_user({"id": display_id, "segment": kind})
    sentry_sdk.set_tag("auth_kind", kind)


def clear_scope_identity() -> None:
    """Reset the Sentry scope user — used on sign-out flows or
    background tasks that shouldn't inherit a previous request's
    identity. No-op without sentry-sdk."""
    if sentry_sdk is None:
        return
    sentry_sdk.set_user(None)


def tag(name: str, value: str | int | None) -> None:
    """Set a Sentry tag on the current scope.

    Cheap shim so handlers don't import sentry_sdk directly. ``value``
    is coerced to string; ``None`` is dropped (Sentry doesn't accept
    null tag values). Designed for bounded-cardinality tags only —
    ``paper_id``, ``claim_id``, ``annotation_type``, etc. **Never** use
    this for unbounded values (request ids, timestamps).
    """
    if sentry_sdk is None or value is None:
        return
    sentry_sdk.set_tag(name, str(value))


def _log_format() -> str:
    return os.environ.get("RRXIV_LOG_FORMAT", "plain").strip().lower()


class RequestLoggingMiddleware:
    """ASGI middleware emitting one access-log record per request.

    Emits JSON when ``RRXIV_LOG_FORMAT=json`` (production); compact
    plain text otherwise. Records duration, status, auth context, and
    a generated request_id. The request_id is attached to the Sentry
    scope too, so Sentry events + log lines can be correlated.

    Skips the noisy `/metrics` and `/api/v0/version` endpoints (Fly
    healthcheck hits version every 30s; Prometheus scrapes /metrics
    every 15s). Both surface plenty of signal via their respective
    integrations.
    """

    _NOISY_PATHS: ClassVar[set[str]] = {"/metrics", "/api/v0/version"}

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in self._NOISY_PATHS:
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        request_id = uuid.uuid4().hex[:12]
        # Stash on ASGI scope state so the error handler can echo it back
        # to the client (correlates with fly logs + Sentry event_id).
        scope.setdefault("state", {})
        scope["state"]["request_id"] = request_id
        if sentry_sdk is not None:
            sentry_sdk.set_tag("request_id", request_id)

        status_holder: dict[str, int] = {"code": 0}

        async def _send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status_holder["code"] = message.get("status", 0)
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            authed = None
            # The auth dependency, when it ran, stashed AuthedRequest on
            # request.state.authed; we read via the scope's state dict.
            state = scope.get("state") or {}
            authed = state.get("authed")
            identity = getattr(authed, "identity", None) if authed else None
            kind, ident_id = identity_descriptor(identity)

            # Bump the per-route Prometheus counter. Pull the route
            # template (e.g. "/api/v0/papers/{paper_id}") off the scope
            # so cardinality stays bounded — the literal path embeds
            # the paper_id and would create a label per paper.
            route = scope.get("route")
            path_pattern = getattr(route, "path", None) or path
            try:
                from rrxiv.server.metrics import record_http_request

                record_http_request(
                    method=scope.get("method") or "?",
                    path_pattern=path_pattern,
                    status=status_holder["code"],
                    auth_kind=kind,
                )
            except Exception:
                pass

            record: dict[str, Any] = {
                "ts": time.time(),
                "request_id": request_id,
                "method": scope.get("method"),
                "path": path,
                "path_pattern": path_pattern,
                "status": status_holder["code"],
                "duration_ms": duration_ms,
                "auth_kind": kind,
                "auth_id": ident_id,
            }
            if _log_format() == "json":
                _access_log.info(json.dumps(record, separators=(",", ":")))
            else:
                _access_log.info(
                    "%s %s -> %s in %sms auth=%s",
                    record["method"],
                    record["path"],
                    record["status"],
                    record["duration_ms"],
                    record["auth_kind"],
                )
