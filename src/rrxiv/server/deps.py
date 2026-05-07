"""FastAPI dependency injection — the "is the request authentic?"
pipeline that all routes lean on.

Three identity tiers per RRP-0005, plus signed-write enforcement
per RRP-0007.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from fastapi import Header, Request

from rrxiv.client.signatures import (
    SignatureVerificationError,
    verify_request_signature,
)
from rrxiv.server.errors import (
    forbidden,
    rate_limited,
    unauthorized,
)
from rrxiv.server.settings import ServerSettings
from rrxiv.server.store import (
    AgentIdentity,
    AnonymousIdentity,
    Identity,
    OrcidIdentity,
    Store,
)


@dataclass(frozen=True, slots=True)
class AuthedRequest:
    """The result of a successful auth pass.

    Routes use this as the canonical source of "who's asking" and
    "what tier are they".
    """

    identity: Identity
    token: str

    @property
    def tier(self) -> str:
        if isinstance(self.identity, OrcidIdentity):
            return "orcid"
        if isinstance(self.identity, AgentIdentity):
            return "agent"
        return "anonymous"


def get_store(request: Request) -> Store:
    """Pull the configured Store off the FastAPI app state."""
    store: Store = request.app.state.store
    return store


def get_settings(request: Request) -> ServerSettings:
    settings: ServerSettings = request.app.state.settings
    return settings


def _identity_from_token(store: Store, token: str) -> Identity | None:
    record = store.get_token(token)
    if record is None:
        return None
    if record.expires_at_unix < int(time.time()):
        store.revoke_token(token)
        return None
    return record.identity


async def _ensure_signature_verified_for_writes(
    request: Request,
    settings: ServerSettings,
    store: Store,
    identity: Identity,
) -> None:
    """RRP-0007: agent writes require both bearer and a valid
    Signature. Reads pass through. Non-agent identities are
    unaffected."""
    if request.method.upper() in ("GET", "HEAD"):
        return
    if not isinstance(identity, AgentIdentity):
        return

    body = await request.body()
    httpx_view = httpx.Request(
        method=request.method,
        url=str(request.url),
        headers=list(request.headers.items()),
        content=body,
    )

    def lookup(handle: str) -> object | None:
        rec = store.get_agent(handle)
        if rec is None:
            return None
        # Resolve to a usable Ed25519PublicKey object.
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

    try:
        verified = verify_request_signature(
            request=httpx_view,
            body=body,
            public_key_lookup=lookup,
            clock_skew_seconds=settings.signature_clock_skew_seconds,
        )
    except SignatureVerificationError as e:
        raise unauthorized(f"signature: {e.reason}") from e

    if verified.keyid != identity.handle:
        # The signature was made under a different agent than the bearer.
        raise forbidden(
            f"signature keyid {verified.keyid!r} does not match "
            f"bearer identity {identity.handle!r}"
        )


def require_identity(*, allow_anonymous: bool = True) -> Callable:  # type: ignore[type-arg]
    """Build a dependency that resolves a valid bearer token to an
    identity and (for agent writes) verifies the signature.

    Args:
        allow_anonymous: when False, anonymous tokens are rejected
            with 403 (used on write endpoints).
    """

    async def _dep(
        request: Request,
        authorization: str = Header(default=""),
    ) -> AuthedRequest:
        store = get_store(request)
        settings = get_settings(request)
        if not authorization or not authorization.lower().startswith("bearer "):
            raise unauthorized()
        token = authorization.split(" ", 1)[1].strip()
        identity = _identity_from_token(store, token)
        if identity is None:
            raise unauthorized("token not recognised or expired")

        if isinstance(identity, AnonymousIdentity) and not allow_anonymous:
            raise forbidden("anonymous identities cannot perform this action")

        await _ensure_signature_verified_for_writes(
            request, settings, store, identity
        )

        # Rate limit (sliding window per token, naive).
        rpm = _rpm_for(identity, request.method, settings)
        count = store.record_request(token, int(time.time()))
        if count > rpm:
            raise rate_limited(retry_after=1)

        return AuthedRequest(identity=identity, token=token)

    return _dep


def optional_identity() -> Callable:  # type: ignore[type-arg]
    """Resolve identity if a bearer is present; otherwise return
    ``None``. Used on read endpoints that allow unauthenticated GET
    but want to know the principal for rate-limiting."""

    async def _dep(
        request: Request,
        authorization: str = Header(default=""),
    ) -> AuthedRequest | None:
        if not authorization:
            return None
        store = get_store(request)
        settings = get_settings(request)
        if not authorization.lower().startswith("bearer "):
            return None
        token = authorization.split(" ", 1)[1].strip()
        identity = _identity_from_token(store, token)
        if identity is None:
            return None

        await _ensure_signature_verified_for_writes(
            request, settings, store, identity
        )

        rpm = _rpm_for(identity, request.method, settings)
        count = store.record_request(token, int(time.time()))
        if count > rpm:
            raise rate_limited(retry_after=1)

        return AuthedRequest(identity=identity, token=token)

    return _dep


def _rpm_for(
    identity: Identity, method: str, settings: ServerSettings
) -> int:
    is_write = method.upper() not in ("GET", "HEAD")
    if isinstance(identity, OrcidIdentity):
        return (
            settings.rate_limit_orcid_write_rpm
            if is_write
            else settings.rate_limit_orcid_read_rpm
        )
    if isinstance(identity, AgentIdentity):
        return (
            settings.rate_limit_agent_write_rpm
            if is_write
            else settings.rate_limit_agent_read_rpm
        )
    return settings.rate_limit_anonymous_read_rpm
