"""Tests for HTTP Message Signatures (RFC 9421) per RRP-0007."""

from __future__ import annotations

import time
from base64 import b64encode

import httpx
import pytest

from rrxiv.client.signatures import (
    SIGNATURE_LABEL,
    AgentSigningAuth,
    AgentSigningKey,
    SignatureVerificationError,
    verify_request_signature,
)

# All tests in this module need cryptography (Ed25519).
pytest.importorskip("cryptography")


def _capture_handler(captured: list[httpx.Request]) -> httpx.MockTransport:
    """A MockTransport that just captures requests and returns 200."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Ensure the body is materialised so captured.content works.
        captured.append(request)
        return httpx.Response(
            200,
            content=b'{"ok": true}',
            headers={"content-type": "application/json"},
        )

    return httpx.MockTransport(handler)


def test_signing_attaches_headers_on_post_with_body() -> None:
    key = AgentSigningKey.generate(handle="@my-bot")
    captured: list[httpx.Request] = []
    transport = _capture_handler(captured)

    with httpx.Client(transport=transport) as c:
        c.post(
            "https://rrxiv.com/api/v0/annotations",
            json={"id": "ann-1"},
            auth=AgentSigningAuth(key),
            headers={"Idempotency-Key": "k1"},
        )

    assert len(captured) == 1
    req = captured[0]
    assert "Signature-Input" in req.headers
    assert "Signature" in req.headers
    assert "Content-Digest" in req.headers
    assert req.headers["Content-Digest"].startswith("sha-256=:")
    sig_input = req.headers["Signature-Input"]
    assert SIGNATURE_LABEL + "=" in sig_input
    assert 'keyid="@my-bot"' in sig_input
    assert 'alg="ed25519"' in sig_input


def test_signing_omits_body_components_on_get() -> None:
    key = AgentSigningKey.generate(handle="@my-bot")
    captured: list[httpx.Request] = []
    with httpx.Client(transport=_capture_handler(captured)) as c:
        c.get(
            "https://rrxiv.com/api/v0/papers/p1",
            auth=AgentSigningAuth(key),
        )
    sig_input = captured[0].headers["Signature-Input"]
    assert "content-digest" not in sig_input
    assert "content-type" not in sig_input


def test_signing_includes_idempotency_key_when_present() -> None:
    key = AgentSigningKey.generate(handle="@my-bot")
    captured: list[httpx.Request] = []
    with httpx.Client(transport=_capture_handler(captured)) as c:
        c.post(
            "https://rrxiv.com/api/v0/annotations",
            json={},
            auth=AgentSigningAuth(key),
            headers={"Idempotency-Key": "abc"},
        )
    sig_input = captured[0].headers["Signature-Input"]
    assert "idempotency-key" in sig_input


def test_signing_omits_idempotency_when_absent() -> None:
    key = AgentSigningKey.generate(handle="@my-bot")
    captured: list[httpx.Request] = []
    with httpx.Client(transport=_capture_handler(captured)) as c:
        c.post(
            "https://rrxiv.com/api/v0/annotations",
            json={},
            auth=AgentSigningAuth(key),
        )
    sig_input = captured[0].headers["Signature-Input"]
    assert "idempotency-key" not in sig_input


def test_round_trip_verifies_with_public_key() -> None:
    key = AgentSigningKey.generate(handle="@my-bot")
    captured: list[httpx.Request] = []
    with httpx.Client(transport=_capture_handler(captured)) as c:
        c.post(
            "https://rrxiv.com/api/v0/annotations",
            json={"hello": "world"},
            auth=AgentSigningAuth(key),
            headers={"Idempotency-Key": "k1", "User-Agent": "test"},
        )
    req = captured[0]
    body = req.content

    def lookup(handle: str):  # type: ignore[no-untyped-def]
        return key.public_key if handle == "@my-bot" else None

    result = verify_request_signature(
        request=req, body=body, public_key_lookup=lookup
    )
    assert result.keyid == "@my-bot"
    assert result.created_unix > 0


def test_verification_fails_on_unknown_keyid() -> None:
    key = AgentSigningKey.generate(handle="@my-bot")
    captured: list[httpx.Request] = []
    with httpx.Client(transport=_capture_handler(captured)) as c:
        c.post(
            "https://rrxiv.com/api/v0/annotations",
            json={"x": 1},
            auth=AgentSigningAuth(key),
            headers={"Idempotency-Key": "k"},
        )
    req = captured[0]

    def lookup(handle: str):  # type: ignore[no-untyped-def]
        return None  # unknown

    with pytest.raises(
        SignatureVerificationError,
        match=r"unknown keyid|signature verification failed",
    ):
        verify_request_signature(
            request=req, body=req.content, public_key_lookup=lookup
        )


def test_verification_fails_on_tampered_body() -> None:
    key = AgentSigningKey.generate(handle="@my-bot")
    captured: list[httpx.Request] = []
    with httpx.Client(transport=_capture_handler(captured)) as c:
        c.post(
            "https://rrxiv.com/api/v0/annotations",
            json={"value": "original"},
            auth=AgentSigningAuth(key),
            headers={"Idempotency-Key": "k"},
        )
    req = captured[0]

    def lookup(handle: str):  # type: ignore[no-untyped-def]
        return key.public_key

    # Pass a different body than what the request carries.
    with pytest.raises(SignatureVerificationError, match="Content-Digest mismatch"):
        verify_request_signature(
            request=req,
            body=b'{"value": "tampered"}',
            public_key_lookup=lookup,
        )


def test_verification_fails_on_stale_created() -> None:
    key = AgentSigningKey.generate(handle="@my-bot")
    captured: list[httpx.Request] = []
    with httpx.Client(transport=_capture_handler(captured)) as c:
        c.post(
            "https://rrxiv.com/api/v0/annotations",
            json={},
            auth=AgentSigningAuth(key),
            headers={"Idempotency-Key": "k"},
        )
    req = captured[0]

    def lookup(handle: str):  # type: ignore[no-untyped-def]
        return key.public_key

    # Pretend "now" is 10 minutes after the signature was made.
    far_future = int(time.time()) + 10 * 60
    with pytest.raises(
        SignatureVerificationError, match="created timestamp out of window"
    ):
        verify_request_signature(
            request=req,
            body=req.content,
            public_key_lookup=lookup,
            now_unix=far_future,
        )


def test_verification_fails_on_missing_signature_headers() -> None:
    # Construct a bare request with no Signature-Input.
    req = httpx.Request("POST", "https://rrxiv.com/api/v0/annotations", content=b"{}")

    def lookup(_: str):  # type: ignore[no-untyped-def]
        return None

    with pytest.raises(
        SignatureVerificationError,
        match="missing Signature-Input or Signature header",
    ):
        verify_request_signature(
            request=req, body=req.content, public_key_lookup=lookup
        )


def test_verification_fails_when_body_present_but_no_content_digest() -> None:
    # Manually craft a request with Signature-Input but no Content-Digest.
    req = httpx.Request(
        "POST",
        "https://rrxiv.com/api/v0/annotations",
        content=b"{}",
        headers={
            "Signature-Input": 'rrxiv=("@method");created=1700000000;keyid="@x";alg="ed25519"',
            "Signature": "rrxiv=:" + b64encode(b"\x00" * 64).decode("ascii") + ":",
        },
    )

    def lookup(_: str):  # type: ignore[no-untyped-def]
        return None

    with pytest.raises(
        SignatureVerificationError, match="Content-Digest missing"
    ):
        verify_request_signature(
            request=req, body=req.content, public_key_lookup=lookup
        )


def test_signing_key_round_trip_via_private_bytes() -> None:
    k1 = AgentSigningKey.generate(handle="@x")
    raw = k1.private_key_bytes()
    assert len(raw) == 32

    k2 = AgentSigningKey.from_private_bytes(handle="@x", private_key_bytes=raw)
    # Re-deriving the public key from the private key should match.
    assert k1.public_key_bytes() == k2.public_key_bytes()
