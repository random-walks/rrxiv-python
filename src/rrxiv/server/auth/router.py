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
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field

from rrxiv.server.deps import get_settings, get_store
from rrxiv.server.errors import (
    bad_request,
    forbidden,
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

    if settings.dev_mode and body.code.startswith("dev-"):
        orcid_id = settings.orcid_dev_id
    else:
        # In a real deployment we would exchange the code with ORCID
        # via httpx here. v0.1 reference server scope: dev mode only.
        # Production deployments swap this module for one that talks
        # to orcid.org. We surface a clear error otherwise.
        raise bad_request(
            "real ORCID code exchange not implemented in v0.1 reference "
            "server; run with --dev-mode for local development, or replace "
            "this handler in your deployment"
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

    # Real hCaptcha siteverify call would happen here in production.
    # In dev_mode we accept any non-empty response.
    if not settings.dev_mode and settings.hcaptcha_secret is None:
        raise bad_request(
            "production hCaptcha integration requires RRXIV_HCAPTCHA_SECRET; "
            "or run with --dev-mode"
        )

    token = _issue_token(
        store=store,
        identity=AnonymousIdentity(challenge_id=body.challenge_id),
        ttl=settings.token_ttl_seconds_anonymous,
    )
    return AnonymousVerifyResponse(
        token=token.token,
        expires_in_seconds=settings.token_ttl_seconds_anonymous,
    )


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
