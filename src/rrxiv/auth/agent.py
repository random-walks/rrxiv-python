"""Agent enrollment flow.

Agents (tools, bots, batch jobs) authenticate with an Ed25519 keypair.
The flow:

1. Agent generates an Ed25519 keypair locally (the private key never
   leaves the agent host).
2. Agent submits the public key + an agent handle (e.g.
   ``@my-extractor``) + an enrollment payload signed with the private
   key to ``POST /auth/agent/enroll``.
3. Server verifies the signature, issues a bearer token tied to the
   handle, and returns it.
4. Subsequent reads use the bearer token. For writes, the protocol
   prefers HTTP Message Signatures (RFC 9421) using the same keypair
   — but bearer auth works for reads at agent-tier rate limits.

This module covers steps 2-3. Keypair generation is the caller's
choice — we use ``cryptography`` if available; the helper is optional.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from rrxiv.client.auth import BearerToken
from rrxiv.client.errors import raise_for_status


@dataclass(frozen=True, slots=True)
class AgentEnrollmentRequest:
    """Body of ``POST /auth/agent/enroll``."""

    handle: str
    """Agent handle, e.g. ``@my-extractor``. Must start with ``@``."""

    public_key_b64: str
    """Ed25519 public key, base64 standard-encoded (32 bytes raw → 44
    chars with padding). The server stores this for signature
    verification on subsequent writes."""

    payload_b64: str
    """Base64 of the canonical JSON enrollment payload (see
    :func:`build_enrollment_payload`). Body of the signature."""

    signature_b64: str
    """Base64 Ed25519 signature over ``payload_b64`` (the *bytes* of
    the base64 string, not the decoded payload). The simplification
    avoids re-canonicalisation disagreements."""

    contact: str | None = None
    """Optional email for ops contact in case of abuse."""


@dataclass(frozen=True, slots=True)
class AgentEnrollmentResponse:
    """Server response to a successful enrollment."""

    token: str
    handle: str
    """Echoed back; in v0.1 always equal to the request handle, but
    future server versions may rewrite (e.g. forced lowercasing)."""
    expires_in_seconds: int | None


def build_enrollment_payload(
    *,
    handle: str,
    public_key_b64: str,
    issued_at: int | None = None,
) -> bytes:
    """Build the canonical JSON enrollment payload to be signed.

    The payload binds the handle, the public key, and an issuance
    timestamp. The server enforces a small clock-skew window (typically
    ±5 minutes) to limit replay; clients should re-build with a fresh
    ``issued_at`` if the first attempt is rejected for skew.

    Args:
        handle: agent handle. Must start with ``@``.
        public_key_b64: base64 Ed25519 public key.
        issued_at: unix timestamp; defaults to ``time.time()``.

    Returns:
        UTF-8 bytes of the canonical JSON payload (sorted keys,
        no whitespace).
    """
    if not handle.startswith("@"):
        raise ValueError(f"handle must start with @, got {handle!r}")
    payload = {
        "handle": handle,
        "public_key_b64": public_key_b64,
        "issued_at": issued_at if issued_at is not None else int(time.time()),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_enrollment_payload(
    *,
    payload: bytes,
    private_key_bytes: bytes,
) -> str:
    """Sign the enrollment payload with an Ed25519 private key.

    Args:
        payload: bytes returned by :func:`build_enrollment_payload`.
        private_key_bytes: 32-byte raw Ed25519 private key. Use
            ``cryptography.hazmat.primitives.asymmetric.ed25519`` to
            generate one.

    Returns:
        Base64 standard-encoded signature over the *base64-encoded
        payload*. Yes, the signature is over the base64 string of the
        payload, not the raw payload bytes — this matches what the
        request body carries and avoids re-canonicalisation issues.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
    except ImportError as e:  # pragma: no cover - cryptography is std for agents
        raise RuntimeError(
            "agent enrollment signing requires `cryptography`; "
            "install rrxiv with the [agent] extra"
        ) from e

    key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    payload_b64 = base64.standard_b64encode(payload)
    sig = key.sign(payload_b64)
    return base64.standard_b64encode(sig).decode("ascii")


def build_rotation_payload(
    *,
    handle: str,
    new_public_key_b64: str,
    issued_at: int | None = None,
) -> bytes:
    """Build the canonical rotation payload (RRP-0010).

    Same shape as :func:`build_enrollment_payload` but with
    ``new_public_key_b64`` instead of ``public_key_b64``.
    """
    if not handle.startswith("@"):
        raise ValueError(f"handle must start with @, got {handle!r}")
    payload = {
        "handle": handle,
        "issued_at": issued_at if issued_at is not None else int(time.time()),
        "new_public_key_b64": new_public_key_b64,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True, slots=True)
class AgentKeyRotationRequest:
    """Body of ``POST /auth/agent/{handle}/rotate-key`` (RRP-0010)."""

    new_public_key_b64: str
    rotation_payload_b64: str
    new_signature_b64: str


@dataclass(frozen=True, slots=True)
class AgentKeyRotationResponse:
    handle: str
    public_key_b64: str
    rotated_at_unix: int


def enroll_agent(
    *,
    api_base: str,
    request: AgentEnrollmentRequest,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> BearerToken:
    """Submit an enrollment request and return the issued bearer token.

    Args:
        api_base: rrxiv API base URL.
        request: a fully-built :class:`AgentEnrollmentRequest`. Use
            :func:`build_enrollment_payload` and
            :func:`sign_enrollment_payload` to construct.
        transport: optional ``httpx.BaseTransport`` for tests.
        timeout: request timeout, seconds.

    Raises:
        rrxiv.client.errors.UnauthorizedError: signature invalid.
        rrxiv.client.errors.ForbiddenError: handle already taken or
            not permitted (e.g., reserved prefix).
        rrxiv.client.errors.ValidationError: request payload was
            malformed.
    """
    base = api_base.rstrip("/")
    body: dict[str, Any] = {
        "handle": request.handle,
        "public_key_b64": request.public_key_b64,
        "payload_b64": request.payload_b64,
        "signature_b64": request.signature_b64,
    }
    if request.contact is not None:
        body["contact"] = request.contact
    with httpx.Client(transport=transport, timeout=timeout) as client:
        resp = client.post(f"{base}/auth/agent/enroll", json=body)
    raise_for_status(resp)
    data = resp.json()
    return BearerToken(
        token=data["token"],
        identity_type="agent",
        identity=data["handle"],
    )


def rotate_agent_key(
    *,
    api_base: str,
    handle: str,
    bearer: BearerToken,
    old_signing_key: Any,  # AgentSigningKey; avoid circular import
    new_private_key_bytes: bytes,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> AgentKeyRotationResponse:
    """Rotate an agent's Ed25519 keypair (RRP-0010).

    The request itself is signed with the *old* private key (transport
    signature, RFC 9421). The body carries an *inline* signature from
    the new private key, proving possession of both keys.

    On success, the server has replaced the registered public key.
    Subsequent writes must use the new private key.

    Args:
        old_signing_key: an :class:`rrxiv.client.AgentSigningKey`
            wrapping the current Ed25519 keypair, used to sign the
            HTTP request transport.
        new_private_key_bytes: 32-byte raw Ed25519 private key for
            the new keypair.

    Returns:
        :class:`AgentKeyRotationResponse` echoing the server's view
        of the rotation (handle, new public key, timestamp).

    Raises:
        rrxiv.client.errors.UnauthorizedError: any signature failed
            verification.
        rrxiv.client.errors.ForbiddenError: handle in path doesn't
            match bearer identity.
    """
    from base64 import standard_b64encode

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    new_priv = Ed25519PrivateKey.from_private_bytes(new_private_key_bytes)
    new_pub_bytes = new_priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    new_pub_b64 = standard_b64encode(new_pub_bytes).decode("ascii")

    payload = build_rotation_payload(
        handle=handle, new_public_key_b64=new_pub_b64
    )
    payload_b64 = standard_b64encode(payload).decode("ascii")
    new_sig_bytes = new_priv.sign(payload_b64.encode("ascii"))
    new_sig_b64 = standard_b64encode(new_sig_bytes).decode("ascii")

    body = {
        "new_public_key_b64": new_pub_b64,
        "rotation_payload_b64": payload_b64,
        "new_signature_b64": new_sig_b64,
    }

    base = api_base.rstrip("/")
    from rrxiv.client.signatures import AgentSigningAuth

    with httpx.Client(transport=transport, timeout=timeout) as client:
        resp = client.post(
            f"{base}/auth/agent/{handle}/rotate-key",
            json=body,
            headers={"Authorization": f"Bearer {bearer.token}"},
            auth=AgentSigningAuth(old_signing_key),
        )
    raise_for_status(resp)
    data = resp.json()
    return AgentKeyRotationResponse(
        handle=data["handle"],
        public_key_b64=data["public_key_b64"],
        rotated_at_unix=int(data["rotated_at_unix"]),
    )
