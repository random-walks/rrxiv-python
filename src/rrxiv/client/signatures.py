"""HTTP Message Signatures (RFC 9421) for agent writes per RRP-0007.

Agents enrolled per RRP-0005 sign every POST/PATCH/DELETE with their
Ed25519 private key. Servers verify against the public key registered
under the agent handle. Bearer auth is *also* required — the bearer
identifies the principal; the signature provides tamper-evidence and
replay protection.

Public surface:

- :class:`AgentSigningKey` — wraps an Ed25519 private key + the agent
  handle that resolves to it server-side.
- :class:`AgentSigningAuth` — :class:`httpx.Auth` subclass that signs
  outgoing requests in place. Wires into ``RrxivClient`` and
  ``AsyncRrxivClient`` via the ``auth=`` constructor parameter.
- :func:`verify_request_signature` — server-side verifier helper.

This module thinly wraps the
`http-message-signatures <https://pypi.org/project/http-message-signatures/>`_
library. The wire format is the contract; this is one conforming
implementation. See RRP-0007 for the spec.
"""

from __future__ import annotations

import datetime
import hashlib
from base64 import b64encode
from collections.abc import Generator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

import httpx
from http_message_signatures import (
    HTTPMessageSigner,
    HTTPMessageVerifier,
    HTTPSignatureKeyResolver,
    InvalidSignature,
    algorithms,
)

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

# RRP-0007: signature label is fixed so verifiers know which signature
# is "ours" in a multi-signature request.
SIGNATURE_LABEL = "rrxiv"

# RRP-0007: clock-skew tolerance in seconds. Servers reject signatures
# whose `created` timestamp is more than this off server clock.
DEFAULT_CLOCK_SKEW_SECONDS = 300

# Component lists per RRP-0007. We compute the actual list per-request
# (some components only apply when there's a body).
_BASE_COMPONENTS: tuple[str, ...] = ("@method", "@target-uri", "@authority")
_BODY_COMPONENTS: tuple[str, ...] = ("content-digest", "content-type")
_IDEMPOTENCY_COMPONENT: str = "idempotency-key"


@dataclass(frozen=True, slots=True)
class AgentSigningKey:
    """An agent's Ed25519 keypair plus the handle that names it.

    The handle is what servers use as ``keyid`` to look up the public
    key. Created by :func:`from_private_bytes` (typical for clients
    loading a stored key) or directly with the ``cryptography`` types
    (typical right after enrollment).
    """

    handle: str
    private_key: Ed25519PrivateKey
    public_key: Ed25519PublicKey

    @classmethod
    def from_private_bytes(
        cls, *, handle: str, private_key_bytes: bytes
    ) -> AgentSigningKey:
        """Construct from a 32-byte raw Ed25519 private key."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        priv = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
        return cls(handle=handle, private_key=priv, public_key=priv.public_key())

    @classmethod
    def generate(cls, *, handle: str) -> AgentSigningKey:
        """Generate a fresh keypair. Use during enrollment."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        priv = Ed25519PrivateKey.generate()
        return cls(handle=handle, private_key=priv, public_key=priv.public_key())

    def private_key_bytes(self) -> bytes:
        """Raw 32-byte private key — suitable for credential storage."""
        from cryptography.hazmat.primitives import serialization

        return self.private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def public_key_bytes(self) -> bytes:
        """Raw 32-byte public key."""
        from cryptography.hazmat.primitives import serialization

        return self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )


def _content_digest(body: bytes) -> str:
    """RFC 9530 Content-Digest header value, SHA-256."""
    digest = hashlib.sha256(body).digest()
    return f"sha-256=:{b64encode(digest).decode('ascii')}:"


def _components_for_request(request: httpx.Request) -> tuple[str, ...]:
    """Pick the covered components for ``request`` per RRP-0007.

    Always-on: ``@method @target-uri @authority``. With body:
    ``content-digest content-type``. With idempotency key:
    ``idempotency-key``.
    """
    components = list(_BASE_COMPONENTS)
    body = request.content
    if body:
        components.extend(_BODY_COMPONENTS)
    if "idempotency-key" in request.headers:
        components.append(_IDEMPOTENCY_COMPONENT)
    return tuple(components)


class _SingleAgentKeyResolver(HTTPSignatureKeyResolver):  # type: ignore[misc]
    """Key resolver that hands out one private key, regardless of keyid.

    Clients only ever sign with one key (the one passed to
    :class:`AgentSigningAuth`); the library calls
    :py:meth:`resolve_private_key` with the keyid we set, but we
    already know which key to use. Public-key resolution is also a
    no-op on the client side — we don't verify our own signatures.
    """

    def __init__(self, signing_key: AgentSigningKey) -> None:
        self._signing_key = signing_key

    def resolve_private_key(self, key_id: str) -> Ed25519PrivateKey:
        return self._signing_key.private_key

    def resolve_public_key(self, key_id: str) -> Ed25519PublicKey:
        return self._signing_key.public_key


class AgentSigningAuth(httpx.Auth):
    """``httpx.Auth`` that signs outgoing requests per RRP-0007.

    Add to a client alongside the bearer auth — RRP-0007 mandates
    *both*: the bearer identifies the principal; the signature
    proves tamper-evidence and recency.

    Example::

        from rrxiv.client import RrxivClient, BearerToken
        from rrxiv.client.signatures import AgentSigningKey, AgentSigningAuth

        bearer = BearerToken("agent-tok-xxxx", "agent", "@my-bot")
        signing = AgentSigningKey.from_private_bytes(
            handle="@my-bot", private_key_bytes=...
        )
        client = RrxivClient(
            "https://rrxiv.com/api/v0",
            auth=bearer,
            agent_signing_key=signing,
        )
        ann = client.create_annotation({...})  # auto-signed
    """

    requires_request_body = True
    """Tell httpx to materialize the body before calling auth_flow —
    we need the bytes for content-digest."""

    def __init__(self, signing_key: AgentSigningKey) -> None:
        self._signing_key = signing_key
        self._signer = HTTPMessageSigner(
            signature_algorithm=algorithms.ED25519,
            key_resolver=_SingleAgentKeyResolver(signing_key),
            component_resolver_class=_HttpxComponentResolver,
        )

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        body = request.content
        if body:
            # Insert/overwrite Content-Digest before signing.
            request.headers["Content-Digest"] = _content_digest(body)
        components = _components_for_request(request)
        self._signer.sign(
            request,
            key_id=self._signing_key.handle,
            covered_component_ids=components,
            label=SIGNATURE_LABEL,
            created=datetime.datetime.now(datetime.UTC),
        )
        yield request


# The library's default component resolver expects ``message.url`` to
# be string-coercible, which httpx.URL is, but
# ``CaseInsensitiveDict(message.headers)`` chokes on httpx.Headers in
# some library versions. We supply a thin replacement that just uses
# the httpx-native headers directly.
class _HttpxComponentResolver:
    """httpx-native component resolver. Same surface as the library's
    default but constructed cleanly from an httpx.Request."""

    derived_component_names: ClassVar[frozenset[str]] = frozenset({
        "@method",
        "@target-uri",
        "@authority",
        "@scheme",
        "@request-target",
        "@path",
        "@query",
        "@query-params",
        "@status",
    })

    def __init__(self, message: httpx.Request) -> None:
        self.message = message
        self.url = str(message.url)
        # httpx.Headers is already case-insensitive.
        self.headers = message.headers

    def resolve(self, component_node: Any) -> str:
        from http_message_signatures.exceptions import (
            HTTPMessageSignaturesException,
        )

        cid = str(component_node.value)
        if cid.startswith("@"):
            if cid not in self.derived_component_names:
                raise HTTPMessageSignaturesException(
                    f"unknown derived component {cid!r}"
                )
            method = getattr(self, "get_" + cid[1:].replace("-", "_"))
            return str(method(**component_node.params))
        if cid not in self.headers:
            raise HTTPMessageSignaturesException(
                f"covered header {cid!r} not found in message"
            )
        return str(self.headers[cid])

    def get_method(self) -> str:
        return self.message.method.upper()

    def get_target_uri(self) -> str:
        return self.url

    def get_authority(self) -> str:
        from urllib.parse import urlsplit

        return urlsplit(self.url).netloc.lower()

    def get_scheme(self) -> str:
        from urllib.parse import urlsplit

        return urlsplit(self.url).scheme.lower()

    def get_path(self) -> str:
        from urllib.parse import urlsplit

        return urlsplit(self.url).path

    def get_query(self) -> str:
        from urllib.parse import urlsplit

        return "?" + urlsplit(self.url).query

    def get_request_target(self) -> str:
        from urllib.parse import urlsplit

        s = urlsplit(self.url)
        return s.path + ("?" + s.query if s.query else "")

    def get_query_params(self, *, name: str) -> str:
        from urllib.parse import parse_qs, urlsplit

        from http_message_signatures.exceptions import (
            HTTPMessageSignaturesException,
        )

        params = parse_qs(urlsplit(self.url).query, keep_blank_values=True)
        if name not in params:
            raise HTTPMessageSignaturesException(
                f"query param {name!r} not in URL"
            )
        if len(params[name]) != 1:
            raise HTTPMessageSignaturesException(
                "multi-valued query params not supported"
            )
        return params[name][0]


# ----- server-side verification ----------------------------------------


@dataclass(frozen=True, slots=True)
class VerifiedSignature:
    """Result of a successful signature verification."""

    keyid: str
    """The agent handle the signature was made under."""

    created_unix: int
    """The signing timestamp (seconds since epoch)."""


class SignatureVerificationError(Exception):
    """Raised when an HTTP message signature fails verification.

    The :py:attr:`reason` is a short hint suitable for inclusion in the
    server's RFC 9457 problem-details ``detail`` field.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _ServerKeyResolver(HTTPSignatureKeyResolver):  # type: ignore[misc]
    """Wraps an ``handle -> Ed25519PublicKey`` lookup callable."""

    def __init__(
        self,
        lookup: Any,  # Callable[[str], Ed25519PublicKey | None] but avoid forward import noise
    ) -> None:
        self._lookup = lookup

    def resolve_public_key(self, key_id: str) -> Ed25519PublicKey:
        from http_message_signatures.exceptions import (
            HTTPMessageSignaturesException,
        )

        key = self._lookup(key_id)
        if key is None:
            raise HTTPMessageSignaturesException(f"unknown keyid {key_id!r}")
        return key  # type: ignore[no-any-return]

    def resolve_private_key(self, key_id: str) -> Ed25519PrivateKey:
        raise NotImplementedError("server-side only does verification")


def verify_request_signature(
    *,
    request: httpx.Request,
    body: bytes,
    public_key_lookup: Any,
    clock_skew_seconds: int = DEFAULT_CLOCK_SKEW_SECONDS,
    now_unix: int | None = None,
) -> VerifiedSignature:
    """Verify the ``rrxiv`` HTTP message signature on ``request``.

    Args:
        request: an ``httpx.Request`` view of the incoming request.
            Servers can construct one from raw ASGI scope+body.
        body: the full body bytes (used to verify Content-Digest).
        public_key_lookup: ``(handle: str) -> Ed25519PublicKey | None``.
            Returns the agent's public key, or ``None`` if unknown.
        clock_skew_seconds: tolerance for the ``created`` parameter.
        now_unix: override for "current time" (testing). Defaults to
            ``int(time.time())``.

    Raises:
        SignatureVerificationError: any of the per-step failures from
            RRP-0007 §"Server verification".
    """
    import time

    sig_input = request.headers.get("signature-input")
    sig = request.headers.get("signature")
    if not sig_input or not sig:
        raise SignatureVerificationError("missing Signature-Input or Signature header")

    # Body integrity: recompute the digest and compare.
    if body:
        sent = request.headers.get("content-digest")
        if not sent:
            raise SignatureVerificationError("body present but Content-Digest missing")
        expected = _content_digest(body)
        if sent.strip() != expected:
            raise SignatureVerificationError("Content-Digest mismatch")

    # Validate created window before doing crypto — cheap fail-fast.
    created = _extract_created_unix(sig_input)
    if created is None:
        raise SignatureVerificationError("missing created parameter on signature")
    now = now_unix if now_unix is not None else int(time.time())
    if abs(now - created) > clock_skew_seconds:
        raise SignatureVerificationError(
            "signature created timestamp out of window"
        )

    keyid = _extract_keyid(sig_input)
    if keyid is None:
        raise SignatureVerificationError("missing keyid parameter on signature")

    verifier = HTTPMessageVerifier(
        signature_algorithm=algorithms.ED25519,
        key_resolver=_ServerKeyResolver(public_key_lookup),
        component_resolver_class=_HttpxComponentResolver,
    )
    try:
        results = verifier.verify(request)
    except InvalidSignature as e:
        raise SignatureVerificationError(f"signature verification failed: {e}") from e
    except Exception as e:  # library wraps several internal exception types
        raise SignatureVerificationError(f"signature verification failed: {e}") from e

    # Find our label.
    for r in results:
        if str(r.label) == SIGNATURE_LABEL:
            return VerifiedSignature(keyid=keyid, created_unix=created)
    raise SignatureVerificationError(
        f"no signature with label {SIGNATURE_LABEL!r} present"
    )


def _extract_created_unix(sig_input_header: str) -> int | None:
    """Pull ``created=...`` from the ``rrxiv`` entry of Signature-Input.

    Format: ``rrxiv=("@method" ...);created=1714000000;keyid="..."``.
    Returns int or None.
    """
    from http_message_signatures import http_sfv

    d = http_sfv.Dictionary()
    d.parse(sig_input_header.encode("ascii"))
    entry = d.get(SIGNATURE_LABEL)
    if entry is None:
        return None
    created = entry.params.get("created")
    return int(created) if created is not None else None


def _extract_keyid(sig_input_header: str) -> str | None:
    """Pull ``keyid="..."`` from the ``rrxiv`` entry."""
    from http_message_signatures import http_sfv

    d = http_sfv.Dictionary()
    d.parse(sig_input_header.encode("ascii"))
    entry = d.get(SIGNATURE_LABEL)
    if entry is None:
        return None
    keyid = entry.params.get("keyid")
    return str(keyid) if keyid is not None else None


__all__ = [
    "DEFAULT_CLOCK_SKEW_SECONDS",
    "SIGNATURE_LABEL",
    "AgentSigningAuth",
    "AgentSigningKey",
    "SignatureVerificationError",
    "VerifiedSignature",
    "verify_request_signature",
]
