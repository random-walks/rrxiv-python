"""Signature-verification middleware (Phase 3).

Runs as an ASGI middleware so it can read the request body BEFORE
FastAPI's body-parser (e.g. multipart/form-data UploadFile handling)
consumes it. The middleware:

1. Skips read methods (GET/HEAD).
2. Skips paths that don't carry an Authorization: Bearer header.
3. Resolves the bearer to an identity. If it's an
   :class:`AgentIdentity`, reads the body, verifies the RFC 9421
   signature, and rewinds the body so the downstream app can re-read.
4. On verification failure, returns a 401 problem-details JSON
   directly without invoking the downstream app.

For non-agent identities or read methods, the middleware is a no-op.

The fully-resolved identity is stashed on ``request.state.identity``
and ``request.state.bearer_token`` so the route-level
``require_identity`` dependency can pick them up without re-doing
the work.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from rrxiv.client.signatures import (
    SignatureVerificationError,
    verify_request_signature,
)
from rrxiv.server.store import (
    AgentIdentity,
    Store,
)

_PROBLEM_BASE = "https://rrxiv.com/errors/"


def _problem_response(
    status: int, title: str, detail: str
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "type": _PROBLEM_BASE + title.lower().replace(" ", "-"),
            "title": title,
            "status": status,
            "detail": detail,
        },
        media_type="application/problem+json",
    )


class SignatureVerificationMiddleware:
    """ASGI middleware enforcing RRP-0007 for agent writes."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope["method"].upper()
        if method in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)

        authorization = request.headers.get("authorization", "")
        if not authorization.lower().startswith("bearer "):
            await self.app(scope, receive, send)
            return

        token = authorization.split(" ", 1)[1].strip()
        store: Store = request.app.state.store
        record = store.get_token(token)
        if record is None or record.expires_at_unix < int(time.time()):
            # Let the route-level dependency surface this consistently.
            await self.app(scope, receive, send)
            return

        identity = record.identity
        scope.setdefault("state", {})
        scope["state"]["identity"] = identity
        scope["state"]["bearer_token"] = token

        if not isinstance(identity, AgentIdentity):
            await self.app(scope, receive, send)
            return

        # Read the body once, verify the signature, then proxy the
        # body to the downstream app via a fresh receive callable.
        body = await _read_body(receive)

        httpx_view = httpx.Request(
            method=request.method,
            url=str(request.url),
            headers=list(request.headers.items()),
            content=body,
        )

        def lookup(handle: str) -> Any | None:
            rec = store.get_agent(handle)
            if rec is None:
                return None
            from base64 import b64decode

            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )

            try:
                return Ed25519PublicKey.from_public_bytes(
                    b64decode(rec.public_key_b64)
                )
            except Exception:
                return None

        settings = request.app.state.settings
        try:
            verified = verify_request_signature(
                request=httpx_view,
                body=body,
                public_key_lookup=lookup,
                clock_skew_seconds=settings.signature_clock_skew_seconds,
            )
        except SignatureVerificationError as e:
            response = _problem_response(401, "Unauthorized", f"signature: {e.reason}")
            await response(scope, receive, send)
            return

        if verified.keyid != identity.handle:
            response = _problem_response(
                403,
                "Forbidden",
                f"signature keyid {verified.keyid!r} does not match "
                f"bearer identity {identity.handle!r}",
            )
            await response(scope, receive, send)
            return

        # Re-issue the body to the downstream app.
        async def replay_receive() -> dict[str, Any]:
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay_receive, send)


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.request":
            chunks.append(message.get("body") or b"")
            if not message.get("more_body"):
                break
        elif message["type"] == "http.disconnect":
            break
    return b"".join(chunks)
