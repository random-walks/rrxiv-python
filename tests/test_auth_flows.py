"""Tests for token-acquisition flows in rrxiv.auth.

These exercise the wire format end-to-end against MockRrxivServer —
proving the request/response shapes match between client helpers and
server handlers, even before there's a real server to integrate with.
"""

from __future__ import annotations

import base64
import json

import pytest

from rrxiv.auth import (
    AgentEnrollmentRequest,
    AnonymousChallengeResponse,
    build_orcid_authorization_url,
    enroll_agent,
    exchange_orcid_code,
    request_anonymous_challenge,
    verify_anonymous_challenge,
)
from rrxiv.auth.agent import build_enrollment_payload
from rrxiv.client.errors import (
    BadRequestError,
    ForbiddenError,
    UnauthorizedError,
    ValidationError,
)
from rrxiv.testing import MockRrxivServer

# ----------------------------- ORCID OAuth -----------------------------


def test_build_orcid_authorization_url_default_state() -> None:
    out = build_orcid_authorization_url(
        api_base="https://rrxiv.com/api/v0/",
        redirect_uri="http://localhost:7654/callback",
    )
    assert out.url.startswith("https://rrxiv.com/api/v0/auth/orcid/start?")
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A7654%2Fcallback" in out.url
    assert "scope=%2Fauthenticate" in out.url
    assert f"state={out.state}" in out.url
    # State is non-empty and reasonably long
    assert len(out.state) > 16


def test_build_orcid_authorization_url_custom_state() -> None:
    out = build_orcid_authorization_url(
        api_base="https://rrxiv.com/api/v0",
        redirect_uri="http://localhost:7654/callback",
        state="my-custom-state",
    )
    assert out.state == "my-custom-state"
    assert "state=my-custom-state" in out.url


def test_exchange_orcid_code_state_mismatch_raises_value_error() -> None:
    with pytest.raises(ValueError, match="state mismatch"):
        exchange_orcid_code(
            api_base="https://rrxiv.com/api/v0",
            code="abc",
            state="different",
            expected_state="expected",
        )


def test_exchange_orcid_code_round_trip() -> None:
    server = MockRrxivServer()
    server.orcid_id_for_code["test-code-123"] = "0000-0001-2345-6789"
    token = exchange_orcid_code(
        api_base="https://rrxiv.com/api/v0",
        code="test-code-123",
        state="s",
        expected_state="s",
        transport=server.transport,
    )
    assert token.identity_type == "orcid"
    assert token.identity == "0000-0001-2345-6789"
    assert token.token == "orcid-tok-test-code-123"


def test_exchange_orcid_code_unknown_code_uses_default_orcid_id() -> None:
    server = MockRrxivServer()
    token = exchange_orcid_code(
        api_base="https://rrxiv.com/api/v0",
        code="unknown",
        state="s",
        expected_state="s",
        transport=server.transport,
    )
    assert token.identity == "0000-0000-0000-0000"


# ----------------------------- Agent enrollment -----------------------------


def test_build_enrollment_payload_canonical() -> None:
    payload = build_enrollment_payload(
        handle="@my-agent",
        public_key_b64="aGVsbG8td29ybGQ=",
        issued_at=1700000000,
    )
    decoded = json.loads(payload)
    assert decoded == {
        "handle": "@my-agent",
        "public_key_b64": "aGVsbG8td29ybGQ=",
        "issued_at": 1700000000,
    }
    # Canonical: sorted keys, no whitespace
    assert b"  " not in payload
    assert b": " not in payload


def test_build_enrollment_payload_rejects_bare_handle() -> None:
    with pytest.raises(ValueError, match="must start with @"):
        build_enrollment_payload(
            handle="my-agent",
            public_key_b64="aGVsbG8=",
            issued_at=1700000000,
        )


def test_sign_enrollment_payload_round_trip_verifies() -> None:
    """End-to-end: generate keypair, sign, verify with public key."""
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    from rrxiv.auth.agent import sign_enrollment_payload

    priv = Ed25519PrivateKey.generate()
    priv_bytes = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    pub_b64 = base64.standard_b64encode(pub_bytes).decode("ascii")
    payload = build_enrollment_payload(
        handle="@my-agent",
        public_key_b64=pub_b64,
        issued_at=1700000000,
    )
    sig_b64 = sign_enrollment_payload(
        payload=payload,
        private_key_bytes=priv_bytes,
    )

    # Verify (the protocol signs over the base64 of the payload, not
    # the payload bytes — see sign_enrollment_payload docstring).
    pub = Ed25519PublicKey.from_public_bytes(pub_bytes)
    pub.verify(
        base64.standard_b64decode(sig_b64),
        base64.standard_b64encode(payload),
    )


def test_enroll_agent_round_trip() -> None:
    server = MockRrxivServer()
    payload = build_enrollment_payload(
        handle="@test-bot",
        public_key_b64="dGVzdC1wdWJrZXk=",
        issued_at=1700000000,
    )
    request = AgentEnrollmentRequest(
        handle="@test-bot",
        public_key_b64="dGVzdC1wdWJrZXk=",
        payload_b64=base64.standard_b64encode(payload).decode("ascii"),
        signature_b64="ZmFrZS1zaWdu",  # fake; mock doesn't verify
        contact="ops@example.com",
    )
    token = enroll_agent(
        api_base="https://rrxiv.com/api/v0",
        request=request,
        transport=server.transport,
    )
    assert token.identity_type == "agent"
    assert token.identity == "@test-bot"
    assert token.token == "agent-tok-test-bot"
    assert "@test-bot" in server.taken_agent_handles


def test_enroll_agent_handle_collision_raises_forbidden() -> None:
    server = MockRrxivServer()
    server.taken_agent_handles.add("@taken")
    request = AgentEnrollmentRequest(
        handle="@taken",
        public_key_b64="cGsx",
        payload_b64="cGF5",
        signature_b64="c2ln",
    )
    with pytest.raises(ForbiddenError):
        enroll_agent(
            api_base="https://rrxiv.com/api/v0",
            request=request,
            transport=server.transport,
        )


def test_enroll_agent_bare_handle_rejected_by_server() -> None:
    server = MockRrxivServer()
    request = AgentEnrollmentRequest(
        handle="bare",
        public_key_b64="cGsx",
        payload_b64="cGF5",
        signature_b64="c2ln",
    )
    with pytest.raises(ValidationError):
        enroll_agent(
            api_base="https://rrxiv.com/api/v0",
            request=request,
            transport=server.transport,
        )


# ----------------------------- Anonymous attestation -----------------------------


def test_anonymous_challenge_round_trip() -> None:
    server = MockRrxivServer()
    challenge = request_anonymous_challenge(
        api_base="https://rrxiv.com/api/v0",
        transport=server.transport,
    )
    assert challenge.challenge_type == "hcaptcha"
    assert challenge.site_key == server.anonymous_site_key
    assert challenge.expires_in_seconds == 300
    assert challenge.challenge_id in server.live_anonymous_challenges

    token = verify_anonymous_challenge(
        api_base="https://rrxiv.com/api/v0",
        response=AnonymousChallengeResponse(
            challenge_id=challenge.challenge_id,
            response="solved-token-from-widget",
        ),
        transport=server.transport,
    )
    assert token.identity_type == "anonymous"
    assert token.identity is None
    assert token.token == f"anon-tok-{challenge.challenge_id}"
    # Single-use: challenge no longer live
    assert challenge.challenge_id not in server.live_anonymous_challenges


def test_anonymous_verify_with_unknown_challenge_id() -> None:
    server = MockRrxivServer()
    with pytest.raises(UnauthorizedError):
        verify_anonymous_challenge(
            api_base="https://rrxiv.com/api/v0",
            response=AnonymousChallengeResponse(
                challenge_id="never-issued",
                response="solved",
            ),
            transport=server.transport,
        )


def test_anonymous_verify_replay_rejected() -> None:
    server = MockRrxivServer()
    challenge = request_anonymous_challenge(
        api_base="https://rrxiv.com/api/v0",
        transport=server.transport,
    )
    response = AnonymousChallengeResponse(
        challenge_id=challenge.challenge_id,
        response="solved",
    )
    verify_anonymous_challenge(
        api_base="https://rrxiv.com/api/v0",
        response=response,
        transport=server.transport,
    )
    # second time with same response
    with pytest.raises(UnauthorizedError):
        verify_anonymous_challenge(
            api_base="https://rrxiv.com/api/v0",
            response=response,
            transport=server.transport,
        )


def test_anonymous_verify_missing_response_raises_400() -> None:
    server = MockRrxivServer()
    challenge = request_anonymous_challenge(
        api_base="https://rrxiv.com/api/v0",
        transport=server.transport,
    )
    with pytest.raises(BadRequestError):
        verify_anonymous_challenge(
            api_base="https://rrxiv.com/api/v0",
            response=AnonymousChallengeResponse(
                challenge_id=challenge.challenge_id,
                response="",
            ),
            transport=server.transport,
        )
