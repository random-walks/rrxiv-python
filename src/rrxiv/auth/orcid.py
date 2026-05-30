"""ORCID OAuth 2.0 flow.

The rrxiv server is the OAuth client; orcid.org is the IdP. The user
points their browser at the authorization URL, approves the scope, and
orcid.org redirects to the rrxiv server with a code that the server
exchanges for an ORCID iD + access token. The server then mints its
own bearer token tied to that ORCID iD and returns it to the caller.

This module provides:

- :func:`build_orcid_authorization_url` — construct the URL to open in
  the user's browser. Pure function; no network.
- :func:`exchange_orcid_code` — given the authorization code that
  orcid.org redirected to your ``redirect_uri``, exchange it at the
  rrxiv server's ``/auth/orcid/callback`` endpoint for a
  :class:`rrxiv.client.BearerToken`.

The OAuth dance itself (running a local listener for the redirect, or
having the user paste the code back) is the caller's responsibility.
The most common shapes are covered in
``spec/0007-api.md`` §"Auth model".
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from rrxiv.client.auth import BearerToken
from rrxiv.client.errors import raise_for_status


@dataclass(frozen=True, slots=True)
class OrcidAuthorizationUrl:
    """The URL to open in the user's browser plus the state value.

    The caller must verify that the ``state`` returned by orcid.org
    matches ``state`` here — otherwise the authorization code may be
    cross-site forgery.
    """

    url: str
    state: str


def build_orcid_authorization_url(
    *,
    api_base: str,
    redirect_uri: str,
    scope: str = "/authenticate",
    state: str | None = None,
) -> OrcidAuthorizationUrl:
    """Construct the URL the user opens in their browser.

    The rrxiv server proxies to orcid.org, so this URL targets
    ``{api_base}/auth/orcid/start`` rather than orcid.org directly.
    The server handles client-id rotation, scope validation, etc.

    Args:
        api_base: rrxiv API base URL, e.g. ``https://rrxiv.com/api/v0``.
            Trailing slash optional.
        redirect_uri: where orcid.org should redirect after auth. Must
            be one the rrxiv server has registered with orcid.org —
            typically a ``localhost:<port>`` your CLI is listening on.
        scope: ORCID OAuth scope. ``/authenticate`` is the minimum
            (just establishes identity).
        state: CSRF token. If ``None``, one is generated.
    """
    state_val = state if state is not None else secrets.token_urlsafe(24)
    base = api_base.rstrip("/")
    qs = urlencode(
        {"redirect_uri": redirect_uri, "scope": scope, "state": state_val}
    )
    return OrcidAuthorizationUrl(url=f"{base}/auth/orcid/start?{qs}", state=state_val)


@dataclass(frozen=True, slots=True)
class OrcidTokenResponse:
    """What ``/auth/orcid/callback`` returns on success."""

    token: str
    orcid_id: str
    expires_in_seconds: int | None


def exchange_orcid_code(
    *,
    api_base: str,
    code: str,
    state: str,
    expected_state: str,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> BearerToken:
    """Exchange an OAuth authorization code for a rrxiv bearer token.

    Args:
        api_base: rrxiv API base URL.
        code: the ``code`` query param orcid.org redirected with.
        state: the ``state`` query param orcid.org redirected with.
        expected_state: the ``state`` value from
            :class:`OrcidAuthorizationUrl`. Mismatch raises
            :class:`ValueError`.
        transport: optional ``httpx.BaseTransport`` for tests.
        timeout: request timeout, seconds.

    Raises:
        ValueError: state mismatch (CSRF protection).
        rrxiv.client.errors.UnauthorizedError: code rejected by ORCID
            or the rrxiv server.
    """
    if state != expected_state:
        raise ValueError("OAuth state mismatch — possible CSRF; rejecting code")

    base = api_base.rstrip("/")
    with httpx.Client(transport=transport, timeout=timeout) as client:
        resp = client.post(
            f"{base}/auth/orcid/callback",
            json={"code": code, "state": state},
        )
    raise_for_status(resp)
    body = resp.json()
    return BearerToken(
        token=body["token"],
        identity_type="orcid",
        identity=body["orcid_id"],
    )


# ----------------------------- ORCID key binding (RRP-0024) -----------------------------
#
# A user who has an ORCID bearer can bind one or more Ed25519 public keys to
# their ORCID iD, then sign each write (RFC 9421) with the matching private
# key — so a stolen bearer alone can no longer forge a write. The proof-of-
# possession mirrors agent enrollment: the new key signs the *base64 of the
# canonical payload* (see rrxiv.auth.sign_enrollment_payload), and the server
# checks purpose/orcid_id/public_key/clock-window/nonce. Keypair generation +
# signing live in the CLI (rrxiv.cli.auth), which has the optional crypto dep;
# this module stays crypto-free (payload builder + HTTP only).


@dataclass(frozen=True, slots=True)
class OrcidKeyBindRequest:
    """Body of ``POST /auth/orcid/keys`` (RRP-0024)."""

    public_key_b64: str
    payload_b64: str
    signature_b64: str
    label: str = ""


@dataclass(frozen=True, slots=True)
class OrcidKeyRecord:
    """A bound ORCID signing-key record as returned by the server.

    ``key_id`` is server-minted, of the form ``key:<32-hex>``; it is the
    RFC-9421 ``keyid`` used when signing subsequent writes.
    """

    orcid_id: str
    key_id: str
    public_key_b64: str
    label: str
    created_at_unix: int
    revoked_at_unix: int | None = None


def build_key_binding_payload(
    *,
    orcid_id: str,
    public_key_b64: str,
    nonce: str,
    issued_at: int | None = None,
) -> bytes:
    """Build the canonical proof-of-possession payload for key binding.

    The bound key's private half signs the *base64* of these bytes (via
    :func:`rrxiv.auth.sign_enrollment_payload`) to prove possession. The
    server validates ``purpose``, that ``orcid_id`` matches the calling
    bearer, that ``public_key_b64`` matches the outer field, an
    ``issued_at_unix`` clock-skew window, and a non-empty ``nonce``.

    Returns UTF-8 bytes of the canonical JSON (sorted keys, no whitespace).
    """
    payload = {
        "purpose": "orcid_key_binding",
        "orcid_id": orcid_id,
        "public_key_b64": public_key_b64,
        "issued_at_unix": issued_at if issued_at is not None else int(time.time()),
        "nonce": nonce,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _record_from_dict(d: dict[str, Any]) -> OrcidKeyRecord:
    return OrcidKeyRecord(
        orcid_id=d["orcid_id"],
        key_id=d["key_id"],
        public_key_b64=d["public_key_b64"],
        label=d.get("label", ""),
        created_at_unix=int(d["created_at_unix"]),
        revoked_at_unix=(
            int(d["revoked_at_unix"]) if d.get("revoked_at_unix") else None
        ),
    )


def bind_orcid_key(
    *,
    api_base: str,
    bearer: BearerToken,
    request: OrcidKeyBindRequest,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> OrcidKeyRecord:
    """Bind an Ed25519 public key to the calling ORCID identity.

    Requires a fresh ORCID ``bearer``. Build ``request`` from a freshly
    generated keypair with :func:`build_key_binding_payload` +
    :func:`rrxiv.auth.sign_enrollment_payload`.

    Raises:
        rrxiv.client.errors.UnauthorizedError: bearer invalid, or the
            proof-of-possession signature didn't verify.
        rrxiv.client.errors.ForbiddenError: caller isn't an ORCID identity.
        rrxiv.client.errors.ValidationError: payload malformed.
    """
    base = api_base.rstrip("/")
    body: dict[str, Any] = {
        "public_key_b64": request.public_key_b64,
        "label": request.label,
        "payload_b64": request.payload_b64,
        "signature_b64": request.signature_b64,
    }
    with httpx.Client(transport=transport, timeout=timeout) as client:
        resp = client.post(
            f"{base}/auth/orcid/keys",
            json=body,
            headers={"Authorization": f"Bearer {bearer.token}"},
        )
    raise_for_status(resp)
    return _record_from_dict(resp.json())


def list_orcid_keys(
    *,
    api_base: str,
    bearer: BearerToken,
    include_revoked: bool = False,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> list[OrcidKeyRecord]:
    """List the calling ORCID's bound signing keys (active set by default)."""
    base = api_base.rstrip("/")
    with httpx.Client(transport=transport, timeout=timeout) as client:
        resp = client.get(
            f"{base}/auth/orcid/keys",
            params={"include_revoked": "true" if include_revoked else "false"},
            headers={"Authorization": f"Bearer {bearer.token}"},
        )
    raise_for_status(resp)
    return [_record_from_dict(r) for r in resp.json().get("keys", [])]


def revoke_orcid_key(
    *,
    api_base: str,
    bearer: BearerToken,
    key_id: str,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> None:
    """Soft-revoke a bound key by id.

    Raises:
        rrxiv.client.errors.NotFoundError: key doesn't exist or isn't the
            caller's (the server doesn't leak other users' key existence).
    """
    base = api_base.rstrip("/")
    with httpx.Client(transport=transport, timeout=timeout) as client:
        resp = client.request(
            "DELETE",
            f"{base}/auth/orcid/keys/{key_id}",
            headers={"Authorization": f"Bearer {bearer.token}"},
        )
    raise_for_status(resp)
