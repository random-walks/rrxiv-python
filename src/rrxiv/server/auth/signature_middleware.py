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
    OrcidIdentity,
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

        # Anonymous identities are never signed; bearer-only auth.
        # ORCID identities MAY sign (RRP-0024 bound-key flow) but are
        # not required to — bearer-only writes still work.
        # AgentIdentity REQUIRES a signature per RRP-0007.
        has_signature = "signature" in {h.lower() for h in request.headers.keys()}
        if not isinstance(identity, AgentIdentity) and not has_signature:
            # Non-agent identity with no signature → bearer auth.
            # Body passthrough; the route-level dependency does the
            # rest of the auth work (rate limiting, anon checks, etc.)
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

        def lookup(keyid: str) -> Any | None:
            """Resolve a signature keyid → Ed25519 public key.

            Polymorphic per RRP-0024: ``key:<32-hex>`` → OrcidKeyRecord
            lookup (rejects revoked keys); any other keyid → agent
            handle lookup (existing behaviour).
            """
            from base64 import b64decode

            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )

            pub_b64: str | None = None
            if keyid.startswith("key:"):
                rec_orcid = store.get_orcid_key(keyid)
                if rec_orcid is None or rec_orcid.revoked_at_unix is not None:
                    return None
                pub_b64 = rec_orcid.public_key_b64
            else:
                rec_agent = store.get_agent(keyid)
                if rec_agent is not None:
                    pub_b64 = rec_agent.public_key_b64
            if pub_b64 is None:
                return None
            try:
                return Ed25519PublicKey.from_public_bytes(b64decode(pub_b64))
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

        # Polymorphic identity-vs-keyid check (RRP-0024). Reject any
        # mismatch where the signing key doesn't belong to the bearer's
        # identity scope.
        mismatch_reason: str | None = None
        if isinstance(identity, AgentIdentity):
            if verified.keyid.startswith("key:"):
                mismatch_reason = (
                    f"agent bearer cannot use ORCID-bound keyid {verified.keyid!r}"
                )
            elif verified.keyid != identity.handle:
                mismatch_reason = (
                    f"signature keyid {verified.keyid!r} does not match "
                    f"agent handle {identity.handle!r}"
                )
        elif isinstance(identity, OrcidIdentity):
            if not verified.keyid.startswith("key:"):
                mismatch_reason = (
                    f"ORCID bearer cannot use agent keyid {verified.keyid!r}"
                )
            else:
                rec_orcid = store.get_orcid_key(verified.keyid)
                if rec_orcid is None:
                    mismatch_reason = f"key {verified.keyid!r} not found"
                elif rec_orcid.revoked_at_unix is not None:
                    mismatch_reason = f"key {verified.keyid!r} is revoked"
                elif rec_orcid.orcid_id != identity.orcid_id:
                    mismatch_reason = (
                        f"key {verified.keyid!r} not bound to bearer's ORCID iD"
                    )
        else:
            # Anonymous identity with a signature is nonsensical.
            mismatch_reason = "anonymous identities cannot sign requests"

        if mismatch_reason is not None:
            response = _problem_response(
                403,
                "Forbidden",
                mismatch_reason,
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
