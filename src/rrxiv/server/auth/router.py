"""Auth router — implements RRP-0005 endpoints.

Five endpoints:

- ``GET  /auth/orcid/start``         — redirect to ORCID (or to the
  CLI loopback in dev/dev-orcid mode)
- ``POST /auth/orcid/callback``      — exchange code for token
- ``POST /auth/orcid/exchange-paste`` — RRP-0006 paste-fallback
- ``POST /auth/agent/enroll``        — Ed25519 enrollment
- ``POST /auth/anonymous/challenge`` — issue a challenge
- ``POST /auth/anonymous/verify``    — exchange solved challenge for token

The `dev_mode` setting bypasses real ORCID/hCaptcha integration —
real Ed25519 signature verification stays on for agent enrollment.
"""

from __future__ import annotations

import secrets
import time
import uuid
from base64 import b64decode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, EmailStr, Field

from rrxiv.server.auth.templates import (
    render_anonymous_hcaptcha,
    render_orcid_paste,
)
from rrxiv.server.deps import get_settings, get_store
from rrxiv.server.errors import (
    bad_request,
    forbidden,
    not_found,
    unauthorized,
    validation_error,
)
from rrxiv.server.settings import ServerSettings
from rrxiv.server.store import (
    AgentIdentity,
    AgentRecord,
    AnonymousIdentity,
    OrcidIdentity,
    Store,
    TokenRecord,
)
from rrxiv.server.store.protocol import (
    AnonymousChallengeRecord,
    PasteCodeEntry,
)

router = APIRouter(prefix="/auth", tags=["Auth"])


# ----------------------------- ORCID -----------------------------


@router.get("/orcid/start")
def orcid_start(
    request: Request,
    redirect_uri: str = Query(...),
    state: str = Query(...),
    scope: str = Query("/authenticate"),
) -> RedirectResponse:
    """Redirect the user to ORCID (or simulate it in dev mode).

    In dev_mode=True, immediately redirect to ``redirect_uri?code=dev-…``
    so a CLI flow can complete entirely against the local server.
    """
    settings: ServerSettings = request.app.state.settings
    if settings.dev_mode:
        # Simulate ORCID's redirect with a dev code.
        code = f"dev-{uuid.uuid4().hex[:8]}"
        sep = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(
            url=f"{redirect_uri}{sep}code={code}&state={state}",
            status_code=302,
        )
    if not settings.orcid_client_id:
        raise bad_request(
            "server has no ORCID client_id configured; "
            "set RRXIV_ORCID_CLIENT_ID or enable dev mode"
        )
    # Real ORCID flow: redirect to the IdP.
    from urllib.parse import urlencode

    qs = urlencode(
        {
            "client_id": settings.orcid_client_id,
            "response_type": "code",
            "scope": scope,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return RedirectResponse(
        url=f"{settings.orcid_authorize_url}?{qs}", status_code=302
    )


class OrcidCallbackBody(BaseModel):
    code: str
    state: str
    redirect_uri: str | None = None
    """The redirect_uri the web client sent in the authorize step.

    When present, the server uses it for the token-exchange call to
    ORCID (which OAuth requires to be byte-identical to the authorize
    step's redirect_uri per RFC 6749 §4.1.3). When absent, falls back
    to ``RRXIV_ORCID_REDIRECT_URI``.

    Origin-agnostic clients (a web app that runs on both ``rrxiv.com``
    and ``www.rrxiv.com``) must thread this through or the token
    exchange 401s on the mismatch.
    """


class OrcidCallbackResponse(BaseModel):
    token: str
    orcid_id: str
    expires_in_seconds: int


@router.post("/orcid/callback", response_model=OrcidCallbackResponse)
def orcid_callback(
    body: OrcidCallbackBody,
    request: Request,
) -> OrcidCallbackResponse:
    settings: ServerSettings = request.app.state.settings
    store: Store = request.app.state.store

    orcid_id = _resolve_orcid_id_from_code(
        settings, body.code, redirect_uri=body.redirect_uri
    )

    token = _issue_token(
        store=store,
        identity=OrcidIdentity(orcid_id=orcid_id),
        ttl=settings.token_ttl_seconds_orcid,
    )
    return OrcidCallbackResponse(
        token=token.token,
        orcid_id=orcid_id,
        expires_in_seconds=settings.token_ttl_seconds_orcid,
    )


@router.get("/orcid/render", response_class=HTMLResponse)
def orcid_render(
    request: Request,
    code: str = Query(..., description="ORCID-issued OAuth code."),
    state: str = Query(""),
) -> HTMLResponse:
    """Paste-back page (RRP-0006).

    Server-side render of the paste code that the user copies into a
    CLI on a different host. Resolves the ORCID iD from ``code`` (in
    dev mode this is the dev iD; in prod it round-trips through the
    real ORCID OAuth dance), mints a single-use paste code, persists
    it, and renders.

    The ORCID OAuth ``redirect_uri`` for the paste-back flow points
    at this endpoint.
    """
    settings: ServerSettings = request.app.state.settings
    store: Store = request.app.state.store

    orcid_id = _resolve_orcid_id_from_code(settings, code)

    paste_code = _mint_paste_code()
    now = int(time.time())
    store.add_paste_code(
        PasteCodeEntry(
            code=paste_code,
            orcid_id=orcid_id,
            issued_at_unix=now,
            expires_at_unix=now + 300,
        )
    )

    body = render_orcid_paste(
        code=paste_code, orcid_id=orcid_id, expires_in_minutes=5
    )
    return HTMLResponse(content=body)


def _mint_paste_code() -> str:
    """Build a human-friendly paste code: ``RRXIV-<4hex>-<4hex>``."""
    raw = secrets.token_hex(4).upper()
    return f"RRXIV-{raw[:4]}-{raw[4:]}"


def _resolve_orcid_id_from_code(
    settings: ServerSettings,
    code: str,
    *,
    redirect_uri: str | None = None,
) -> str:
    """Either accept a dev code or call orcid.org's token endpoint.

    Configuration:

    - ``RRXIV_ORCID_CLIENT_ID`` and ``RRXIV_ORCID_CLIENT_SECRET`` must
      be set for real-mode exchange.
    - ``redirect_uri`` must match the URI sent in the authorize step
      (OAuth RFC 6749 §4.1.3 requires byte-identical values). Caller
      should pass the same URI the web client used; falls back to
      ``settings.orcid_redirect_uri`` (env var) for legacy callers.

    Raises :class:`ProblemError` on any failure.
    """
    if settings.dev_mode and code.startswith("dev-"):
        return settings.orcid_dev_id

    if not (settings.orcid_client_id and settings.orcid_client_secret):
        raise bad_request(
            "real ORCID code exchange requires both "
            "RRXIV_ORCID_CLIENT_ID and RRXIV_ORCID_CLIENT_SECRET; "
            "or run with --dev-mode for local development"
        )

    effective_redirect_uri = redirect_uri or settings.orcid_redirect_uri or ""

    import httpx

    try:
        resp = httpx.post(
            settings.orcid_token_url,
            data={
                "client_id": settings.orcid_client_id,
                "client_secret": settings.orcid_client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": effective_redirect_uri,
            },
            headers={"Accept": "application/json"},
            timeout=15.0,
        )
    except httpx.HTTPError as e:
        raise unauthorized(f"ORCID token endpoint unreachable: {e}") from e

    if resp.status_code >= 400:
        raise unauthorized(
            f"ORCID token endpoint returned {resp.status_code}: "
            f"{resp.text[:200]}"
        )

    body = resp.json()
    orcid = body.get("orcid")
    if not orcid:
        raise unauthorized(
            "ORCID token response missing 'orcid' field"
        )
    return str(orcid)


class OrcidPasteBody(BaseModel):
    code: str = Field(..., description="The paste code from /auth/orcid/render")


@router.post("/orcid/exchange-paste", response_model=OrcidCallbackResponse)
def orcid_exchange_paste(
    body: OrcidPasteBody,
    request: Request,
) -> OrcidCallbackResponse:
    """RRP-0006 paste fallback: exchange a paste code for a token."""
    settings: ServerSettings = request.app.state.settings
    store: Store = request.app.state.store

    entry = store.get_paste_code(body.code)
    now = int(time.time())
    if entry is None or entry.consumed or entry.expires_at_unix < now:
        raise unauthorized("paste code unknown, consumed, or expired")
    store.consume_paste_code(body.code)

    token = _issue_token(
        store=store,
        identity=OrcidIdentity(orcid_id=entry.orcid_id),
        ttl=settings.token_ttl_seconds_orcid,
    )
    return OrcidCallbackResponse(
        token=token.token,
        orcid_id=entry.orcid_id,
        expires_in_seconds=settings.token_ttl_seconds_orcid,
    )


# ----------------------------- Agent enrollment -----------------------------

_HANDLE_PATTERN = r"^@[a-z0-9][-a-z0-9]{0,31}$"


class AgentEnrollBody(BaseModel):
    handle: str = Field(..., pattern=_HANDLE_PATTERN)
    public_key_b64: str
    payload_b64: str
    signature_b64: str
    contact: EmailStr | None = None


class AgentEnrollResponse(BaseModel):
    token: str
    handle: str
    expires_in_seconds: int


@router.post(
    "/agent/enroll", response_model=AgentEnrollResponse, status_code=201
)
def agent_enroll(
    body: AgentEnrollBody,
    request: Request,
) -> AgentEnrollResponse:
    settings: ServerSettings = request.app.state.settings
    store: Store = request.app.state.store

    if store.get_agent(body.handle) is not None:
        raise forbidden(f"handle {body.handle!r} already taken")

    # Verify the enrollment signature (RRP-0005). Always real, never
    # stubbed — this is the security primitive we care about.
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )

        pub = Ed25519PublicKey.from_public_bytes(b64decode(body.public_key_b64))
        pub.verify(
            b64decode(body.signature_b64),
            body.payload_b64.encode("ascii"),
        )
    except Exception as e:
        raise unauthorized(f"enrollment signature invalid: {e}") from e

    # Enforce issued_at freshness in the canonical payload.
    import json

    try:
        payload = json.loads(b64decode(body.payload_b64).decode("utf-8"))
    except Exception as e:
        raise validation_error(f"payload not valid base64-encoded JSON: {e}") from e
    issued_at = payload.get("issued_at")
    if not isinstance(issued_at, int):
        raise validation_error("payload.issued_at missing or not an int")
    if abs(int(time.time()) - issued_at) > settings.signature_clock_skew_seconds:
        raise unauthorized("enrollment payload issued_at out of window")
    if payload.get("handle") != body.handle:
        raise validation_error("payload.handle does not match request handle")
    if payload.get("public_key_b64") != body.public_key_b64:
        raise validation_error(
            "payload.public_key_b64 does not match request public_key_b64"
        )

    store.add_agent(
        AgentRecord(
            handle=body.handle,
            public_key_b64=body.public_key_b64,
            contact=str(body.contact) if body.contact else None,
            enrolled_at_unix=int(time.time()),
        )
    )
    token = _issue_token(
        store=store,
        identity=AgentIdentity(handle=body.handle),
        ttl=settings.token_ttl_seconds_agent,
    )
    return AgentEnrollResponse(
        token=token.token,
        handle=body.handle,
        expires_in_seconds=settings.token_ttl_seconds_agent,
    )


# ----------------------------- Anonymous attestation -----------------------------


class AnonymousChallengeResponseModel(BaseModel):
    challenge_id: str
    challenge_type: str
    site_key: str
    expires_in_seconds: int


@router.post(
    "/anonymous/challenge", response_model=AnonymousChallengeResponseModel
)
def anonymous_challenge(request: Request) -> AnonymousChallengeResponseModel:
    settings: ServerSettings = request.app.state.settings
    store: Store = request.app.state.store
    challenge_id = f"chal-{uuid.uuid4().hex[:12]}"
    now = int(time.time())
    store.add_challenge(
        AnonymousChallengeRecord(
            challenge_id=challenge_id,
            challenge_type="hcaptcha",
            site_key=settings.hcaptcha_site_key,
            issued_at_unix=now,
            expires_at_unix=now + settings.challenge_ttl_seconds,
        )
    )
    return AnonymousChallengeResponseModel(
        challenge_id=challenge_id,
        challenge_type="hcaptcha",
        site_key=settings.hcaptcha_site_key,
        expires_in_seconds=settings.challenge_ttl_seconds,
    )


class AnonymousVerifyBody(BaseModel):
    challenge_id: str
    response: str


class AnonymousVerifyResponse(BaseModel):
    token: str
    expires_in_seconds: int


@router.post("/anonymous/verify", response_model=AnonymousVerifyResponse)
def anonymous_verify(
    body: AnonymousVerifyBody,
    request: Request,
) -> AnonymousVerifyResponse:
    settings: ServerSettings = request.app.state.settings
    store: Store = request.app.state.store

    if not body.response.strip():
        raise bad_request("response must not be empty")

    challenge = store.get_challenge(body.challenge_id)
    now = int(time.time())
    if (
        challenge is None
        or challenge.consumed
        or challenge.expires_at_unix < now
    ):
        raise unauthorized("challenge unknown, consumed, or expired")
    store.consume_challenge(body.challenge_id)

    _verify_with_hcaptcha(settings, body.response)

    token = _issue_token(
        store=store,
        identity=AnonymousIdentity(challenge_id=body.challenge_id),
        ttl=settings.token_ttl_seconds_anonymous,
    )
    return AnonymousVerifyResponse(
        token=token.token,
        expires_in_seconds=settings.token_ttl_seconds_anonymous,
    )


@router.get("/anonymous/render", response_class=HTMLResponse)
def anonymous_render(
    request: Request,
    challenge_id: str = Query(...),
    site_key: str = Query(...),
) -> HTMLResponse:
    """Hosts the hCaptcha widget for the anonymous flow (RRP-0006).

    The user solves the puzzle in this page; the resulting response
    token is shown for paste-back into the CLI.
    """
    # Light validation: only known-good challenge IDs render.
    store: Store = request.app.state.store
    challenge = store.get_challenge(challenge_id)
    if challenge is None or challenge.consumed:
        raise not_found("challenge unknown or already consumed")
    body = render_anonymous_hcaptcha(
        site_key=site_key, challenge_id=challenge_id
    )
    return HTMLResponse(content=body)


def _verify_with_hcaptcha(settings: ServerSettings, response: str) -> None:
    """Validate ``response`` against hCaptcha's siteverify endpoint.

    In dev mode any non-empty response is accepted. In prod mode the
    server posts to ``https://api.hcaptcha.com/siteverify`` with the
    secret + response token; non-200 or ``success=false`` raises 401.
    """
    if settings.dev_mode:
        # Already enforced "non-empty" upstream.
        return

    if not settings.hcaptcha_secret:
        raise bad_request(
            "production hCaptcha integration requires RRXIV_HCAPTCHA_SECRET; "
            "or run with --dev-mode"
        )

    import httpx

    try:
        resp = httpx.post(
            "https://api.hcaptcha.com/siteverify",
            data={"secret": settings.hcaptcha_secret, "response": response},
            timeout=10.0,
        )
    except httpx.HTTPError as e:
        raise unauthorized(f"hCaptcha verify endpoint unreachable: {e}") from e

    if resp.status_code >= 400:
        raise unauthorized(f"hCaptcha verify HTTP {resp.status_code}")

    body = resp.json()
    if not body.get("success"):
        codes = ",".join(body.get("error-codes") or [])
        raise unauthorized(f"hCaptcha rejected response: {codes or 'unknown'}")


# ----------------------------- Key rotation (RRP-0010) -----------------------------


class AgentRotateKeyBody(BaseModel):
    new_public_key_b64: str
    rotation_payload_b64: str
    new_signature_b64: str


class AgentRotateKeyResponse(BaseModel):
    handle: str
    public_key_b64: str
    rotated_at_unix: int


@router.post(
    "/agent/{handle}/rotate-key",
    response_model=AgentRotateKeyResponse,
    status_code=201,
)
def agent_rotate_key(
    handle: str,
    body: AgentRotateKeyBody,
    request: Request,
) -> AgentRotateKeyResponse:
    """Rotate an enrolled agent's keypair (RRP-0010).

    Two signatures are required:

    1. Transport signature (RFC 9421) verifying with the *old* public
       key — handled by SignatureVerificationMiddleware before this
       route runs.
    2. Inline ``new_signature_b64`` over ``rotation_payload_b64``,
       verifying with the *new* public key.

    The bearer must resolve to ``handle``. On success, the server
    atomically replaces the registered public key.
    """
    settings: ServerSettings = request.app.state.settings
    store: Store = request.app.state.store

    # Bearer-to-identity resolution. The signature middleware has
    # already verified the transport signature for agent identities.
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise unauthorized("missing bearer token")
    token = auth_header.split(" ", 1)[1].strip()
    record = store.get_token(token)
    if record is None:
        raise unauthorized("token not recognised")
    if not isinstance(record.identity, AgentIdentity):
        raise forbidden("only agent identities can rotate keys")
    if record.identity.handle != handle:
        raise forbidden(
            f"bearer identity {record.identity.handle!r} does not match "
            f"path handle {handle!r}"
        )

    # Verify the inline new-key signature.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey,
    )

    try:
        new_pub = Ed25519PublicKey.from_public_bytes(
            b64decode(body.new_public_key_b64)
        )
        new_pub.verify(
            b64decode(body.new_signature_b64),
            body.rotation_payload_b64.encode("ascii"),
        )
    except Exception as e:
        raise unauthorized(f"new-key signature invalid: {e}") from e

    # Validate the canonical rotation payload.
    import json as _json

    try:
        payload = _json.loads(b64decode(body.rotation_payload_b64).decode("utf-8"))
    except Exception as e:
        raise validation_error(
            f"rotation_payload not valid base64-encoded JSON: {e}"
        ) from e
    if payload.get("handle") != handle:
        raise validation_error("rotation_payload.handle does not match path")
    if payload.get("new_public_key_b64") != body.new_public_key_b64:
        raise validation_error(
            "rotation_payload.new_public_key_b64 does not match request"
        )
    issued_at = payload.get("issued_at")
    if not isinstance(issued_at, int):
        raise validation_error("rotation_payload.issued_at missing or not int")
    if abs(int(time.time()) - issued_at) > settings.signature_clock_skew_seconds:
        raise unauthorized("rotation_payload.issued_at out of window")

    # Atomically replace the registered public key.
    existing = store.get_agent(handle)
    if existing is None:
        raise unauthorized("agent not enrolled")  # shouldn't happen — bearer matched
    store.add_agent(
        AgentRecord(
            handle=handle,
            public_key_b64=body.new_public_key_b64,
            contact=existing.contact,
            enrolled_at_unix=existing.enrolled_at_unix,
        )
    )
    return AgentRotateKeyResponse(
        handle=handle,
        public_key_b64=body.new_public_key_b64,
        rotated_at_unix=int(time.time()),
    )


# ----------------------------- Refresh (RRP-0009) -----------------------------


class RefreshResponse(BaseModel):
    token: str
    expires_in_seconds: int


@router.post("/refresh", response_model=RefreshResponse)
def refresh_token(
    request: Request,
    authorization: str = "",
) -> RefreshResponse:
    """Exchange a still-valid bearer for a fresh one (RRP-0009).

    The Authorization header is the entire input. The server revokes
    the old token atomically and returns a new opaque bearer with
    a refreshed TTL matching the identity tier.

    Anonymous tokens cannot be refreshed (anti-abuse; the user
    re-solves a CAPTCHA).
    """
    settings: ServerSettings = request.app.state.settings
    store: Store = request.app.state.store

    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise unauthorized("missing bearer token")
    old_token = auth_header.split(" ", 1)[1].strip()
    record = store.get_token(old_token)
    if record is None:
        raise unauthorized("token not recognised")
    if record.expires_at_unix < int(time.time()):
        raise unauthorized("token expired; re-login from scratch")

    if isinstance(record.identity, AnonymousIdentity):
        raise forbidden("anonymous tokens cannot be refreshed")

    if isinstance(record.identity, OrcidIdentity):
        ttl = settings.token_ttl_seconds_orcid
    elif isinstance(record.identity, AgentIdentity):
        ttl = settings.token_ttl_seconds_agent
    else:
        ttl = settings.token_ttl_seconds_anonymous

    new = _issue_token(store=store, identity=record.identity, ttl=ttl)
    store.revoke_token(old_token)
    return RefreshResponse(token=new.token, expires_in_seconds=ttl)


# ----------------------------- Helpers -----------------------------


def _issue_token(
    *,
    store: Store,
    identity: object,  # OrcidIdentity | AgentIdentity | AnonymousIdentity
    ttl: int,
) -> TokenRecord:
    """Mint a fresh opaque bearer token bound to ``identity`` and
    persist it in the store."""
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    record = TokenRecord(
        token=token,
        identity=identity,  # type: ignore[arg-type]
        issued_at_unix=now,
        expires_at_unix=now + ttl,
    )
    store.add_token(record)
    return record


# Re-export deps for typing convenience.
__all__ = ["get_settings", "get_store", "router"]
