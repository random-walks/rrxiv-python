"""Tests for ORCID ↔ Ed25519 key binding (RRP-0024).

Covers the three new endpoints (POST/GET/DELETE /auth/orcid/keys), the
proof-of-possession enrollment handshake, and the polymorphic
signature middleware that allows ORCID-bearer writes to be signed by a
bound key.

The critical regression case — "ORCID bearer + forged keyid signature
→ rejected" — is :func:`test_signed_write_with_other_users_key_rejected`.
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from typing import Any

import httpx
import pytest

from rrxiv.auth import exchange_orcid_code
from rrxiv.client import AgentSigningKey
from rrxiv.server import ServerSettings, build_app

pytest.importorskip("fastapi")
pytest.importorskip("cryptography")


def _client() -> tuple[Any, httpx.BaseTransport]:
    from fastapi.testclient import TestClient

    app = build_app(settings=ServerSettings(dev_mode=True))
    test_client = TestClient(app)
    return app, test_client._transport


DEV_ORCID = "0000-0001-0000-DEV1"  # ServerSettings.orcid_dev_id default


def _orcid_bearer(transport: httpx.BaseTransport, orcid_id: str = DEV_ORCID) -> Any:
    """Spin up a fresh ORCID bearer in dev mode. Dev mode resolves every
    code to ``settings.orcid_dev_id`` regardless of the caller-supplied
    value — so the returned bearer always maps to DEV_ORCID unless
    settings overrides it.
    """
    del orcid_id  # accepted for symmetry; dev mode ignores it
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        resp = c.get(
            "/auth/orcid/start",
            params={"redirect_uri": "http://x/cb", "state": "s"},
            follow_redirects=False,
        )
    code = resp.headers["location"].split("code=", 1)[1].split("&")[0]
    return exchange_orcid_code(
        api_base="http://test/api/v0",
        code=code,
        state="s",
        expected_state="s",
        transport=transport,
    )


def _sign_with(sk: AgentSigningKey, data: bytes) -> bytes:
    """Sign ``data`` with the AgentSigningKey's private key directly."""
    return sk.private_key.sign(data)


def _build_bind_payload(orcid_id: str, public_key_b64: str) -> tuple[str, str, AgentSigningKey]:
    """Construct (payload_b64, signature_b64, signing_key) for a binding
    proof-of-possession. The signing_key is returned so the test can
    reuse it for downstream signed writes."""
    sk = AgentSigningKey.generate(handle="placeholder")  # handle ignored
    pub_b64 = base64.b64encode(sk.public_key_bytes()).decode()
    payload = {
        "purpose": "orcid_key_binding",
        "orcid_id": orcid_id,
        "public_key_b64": pub_b64,
        "issued_at_unix": int(time.time()),
        "nonce": secrets.token_hex(16),
    }
    payload_b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    sig = _sign_with(sk, payload_b64.encode("ascii"))
    sig_b64 = base64.b64encode(sig).decode()
    return payload_b64, sig_b64, sk


def _post_bind(
    transport: httpx.BaseTransport,
    bearer: str,
    *,
    label: str = "test-laptop",
    orcid_id: str = DEV_ORCID,
) -> tuple[httpx.Response, AgentSigningKey]:
    sk = AgentSigningKey.generate(handle="placeholder")
    public_key_b64 = base64.b64encode(sk.public_key_bytes()).decode()
    payload = {
        "purpose": "orcid_key_binding",
        "orcid_id": orcid_id,
        "public_key_b64": public_key_b64,
        "issued_at_unix": int(time.time()),
        "nonce": secrets.token_hex(16),
    }
    payload_b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    signature_b64 = base64.b64encode(
        _sign_with(sk, payload_b64.encode("ascii"))
    ).decode()
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        resp = c.post(
            "/auth/orcid/keys",
            headers={"Authorization": f"Bearer {bearer}"},
            json={
                "public_key_b64": public_key_b64,
                "label": label,
                "payload_b64": payload_b64,
                "signature_b64": signature_b64,
            },
        )
    return resp, sk


# ----------------------------- happy path -----------------------------


def test_bind_then_list_then_revoke_round_trip() -> None:
    app, transport = _client()
    bearer = _orcid_bearer(transport)
    resp, _sk = _post_bind(transport, bearer.token, label="blaise-laptop")
    assert resp.status_code == 201, resp.text
    record = resp.json()
    assert record["orcid_id"] == DEV_ORCID
    assert record["key_id"].startswith("key:")
    assert len(record["key_id"]) == len("key:") + 32  # 32 hex
    assert record["label"] == "blaise-laptop"
    assert record["revoked_at_unix"] is None
    key_id = record["key_id"]

    # List shows the new key.
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        resp_list = c.get(
            "/auth/orcid/keys",
            headers={"Authorization": f"Bearer {bearer.token}"},
        )
    assert resp_list.status_code == 200, resp_list.text
    keys = resp_list.json()["keys"]
    assert len(keys) == 1 and keys[0]["key_id"] == key_id

    # Revoke.
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        resp_del = c.delete(
            f"/auth/orcid/keys/{key_id}",
            headers={"Authorization": f"Bearer {bearer.token}"},
        )
    assert resp_del.status_code == 204, resp_del.text

    # Default list now empty; include_revoked surfaces the tombstone.
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        resp_list2 = c.get(
            "/auth/orcid/keys",
            headers={"Authorization": f"Bearer {bearer.token}"},
        )
        resp_list3 = c.get(
            "/auth/orcid/keys?include_revoked=true",
            headers={"Authorization": f"Bearer {bearer.token}"},
        )
    assert resp_list2.json()["keys"] == []
    rev = resp_list3.json()["keys"]
    assert len(rev) == 1 and rev[0]["revoked_at_unix"] is not None


# ----------------------------- security cases -----------------------------


def test_bind_rejected_when_signature_invalid() -> None:
    app, transport = _client()
    bearer = _orcid_bearer(transport)
    # Build a valid payload but corrupt the signature.
    payload_b64, _sig_b64, sk = _build_bind_payload(
        "0009-0002-0561-6499",
        base64.b64encode(b"x" * 32).decode(),  # placeholder, replaced below
    )
    pub_b64 = base64.b64encode(sk.public_key_bytes()).decode()
    bad_sig = base64.b64encode(b"\x00" * 64).decode()
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        resp = c.post(
            "/auth/orcid/keys",
            headers={"Authorization": f"Bearer {bearer.token}"},
            json={
                "public_key_b64": pub_b64,
                "label": "bad-sig",
                "payload_b64": payload_b64,
                "signature_b64": bad_sig,
            },
        )
    assert resp.status_code == 401, resp.text


def test_bind_rejected_when_payload_orcid_mismatches_bearer() -> None:
    app, transport = _client()
    bearer = _orcid_bearer(transport)
    # Build a payload claiming a DIFFERENT ORCID iD (not the dev one).
    sk = AgentSigningKey.generate(handle="x")
    pub_b64 = base64.b64encode(sk.public_key_bytes()).decode()
    payload = {
        "purpose": "orcid_key_binding",
        "orcid_id": "0000-0000-0000-0001",  # not the bearer's ORCID
        "public_key_b64": pub_b64,
        "issued_at_unix": int(time.time()),
        "nonce": secrets.token_hex(16),
    }
    payload_b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    sig_b64 = base64.b64encode(_sign_with(sk, payload_b64.encode("ascii"))).decode()
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        resp = c.post(
            "/auth/orcid/keys",
            headers={"Authorization": f"Bearer {bearer.token}"},
            json={
                "public_key_b64": pub_b64,
                "label": "wrong-orcid",
                "payload_b64": payload_b64,
                "signature_b64": sig_b64,
            },
        )
    assert resp.status_code in (400, 401, 422), resp.text


def test_bind_rejected_for_non_orcid_identity() -> None:
    """An agent bearer cannot bind an ORCID key — the endpoint is
    ORCID-only by design."""
    app, transport = _client()
    # Enrol an agent.
    from rrxiv.auth import AgentEnrollmentRequest, enroll_agent
    from rrxiv.auth.agent import build_enrollment_payload, sign_enrollment_payload

    sk = AgentSigningKey.generate(handle="@test-agent")
    payload_bytes = build_enrollment_payload(
        handle="@test-agent",
        public_key_b64=base64.b64encode(sk.public_key_bytes()).decode(),
    )
    payload_b64 = base64.b64encode(payload_bytes).decode()
    sig_b64 = sign_enrollment_payload(
        payload=payload_bytes, private_key_bytes=sk.private_key_bytes()
    )
    agent_bearer = enroll_agent(
        api_base="http://test/api/v0",
        request=AgentEnrollmentRequest(
            handle="@test-agent",
            public_key_b64=base64.b64encode(sk.public_key_bytes()).decode(),
            payload_b64=payload_b64,
            signature_b64=sig_b64,
        ),
        transport=transport,
    )
    # Try to bind an ORCID key as an agent.
    pub_b64 = base64.b64encode(sk.public_key_bytes()).decode()
    payload = {
        "purpose": "orcid_key_binding",
        "orcid_id": DEV_ORCID,
        "public_key_b64": pub_b64,
        "issued_at_unix": int(time.time()),
        "nonce": secrets.token_hex(16),
    }
    payload_b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    sig_b64 = base64.b64encode(_sign_with(sk, payload_b64.encode("ascii"))).decode()
    # Agent writes MUST be signed (RRP-0007). Use AgentSigningAuth so
    # we get past the middleware; we expect the endpoint itself to
    # 403 because agent identities can't bind ORCID keys.
    from rrxiv.client.signatures import AgentSigningAuth

    signer = AgentSigningAuth(signing_key=sk)
    with httpx.Client(
        transport=transport, base_url="http://test/api/v0", auth=signer
    ) as c:
        resp = c.post(
            "/auth/orcid/keys",
            headers={"Authorization": f"Bearer {agent_bearer.token}"},
            json={
                "public_key_b64": pub_b64,
                "label": "agent-tries-orcid-key",
                "payload_b64": payload_b64,
                "signature_b64": sig_b64,
            },
        )
    assert resp.status_code == 403, resp.text


def test_signed_write_with_other_users_key_rejected() -> None:
    """The Plan-agent regression case: Bob's key is in the store under
    a different ORCID; an attacker uses Alice's bearer + Bob's
    signature. The middleware MUST reject — keyid doesn't belong to
    the bearer's ORCID iD.

    Dev mode resolves every bearer to a single ORCID, so we inject
    Bob's key directly into the store and craft the attack request.
    """
    from rrxiv.client.signatures import AgentSigningAuth
    from rrxiv.server.store import OrcidKeyRecord

    app, transport = _client()
    alice_bearer = _orcid_bearer(transport)  # alice = DEV_ORCID

    # Inject Bob's key directly under a DIFFERENT orcid_id.
    bob_orcid = "0000-0000-0000-0001"
    bob_sk = AgentSigningKey.generate(handle="placeholder")
    bob_key_id = "key:" + secrets.token_hex(16)
    app.state.store.add_orcid_key(
        OrcidKeyRecord(
            orcid_id=bob_orcid,
            key_id=bob_key_id,
            public_key_b64=base64.b64encode(bob_sk.public_key_bytes()).decode(),
            label="bob-laptop",
            created_at_unix=int(time.time()),
            revoked_at_unix=None,
        )
    )

    # Attack: present Alice's bearer + Bob's signature (signs with Bob's
    # private key and labels keyid=bob_key_id). Without the middleware
    # fix this would pass — signature is valid, bearer is valid — but
    # the cross-check catches the keyid/bearer mismatch.
    # AgentSigningAuth uses signing_key.handle as the keyid; rebind Bob's
    # key to the ORCID key_id so the wire shows `keyid=key:...`.
    bob_sk_with_key_id = AgentSigningKey.from_private_bytes(
        handle=bob_key_id, private_key_bytes=bob_sk.private_key_bytes()
    )
    bob_signer = AgentSigningAuth(signing_key=bob_sk_with_key_id)
    with httpx.Client(
        transport=transport, base_url="http://test/api/v0", auth=bob_signer
    ) as c:
        resp = c.post(
            "/auth/orcid/keys",  # any signed write endpoint
            headers={"Authorization": f"Bearer {alice_bearer.token}"},
            json={
                "public_key_b64": "x",
                "label": "x",
                "payload_b64": "x",
                "signature_b64": "x",
            },
        )
    assert resp.status_code == 403, resp.text
    body_lower = resp.text.lower()
    assert (
        "not bound to bearer" in body_lower
        or "does not match" in body_lower
    )


def test_orcid_bearer_without_signature_still_works() -> None:
    """Bearer-only auth must continue to work for ORCID identities
    that haven't bound a key. Backward-compat invariant."""
    app, transport = _client()
    bearer = _orcid_bearer(transport)
    # POST /auth/orcid/keys requires bearer; the absence of a Signature
    # header doesn't break that path.
    resp, _ = _post_bind(transport, bearer.token, label="no-sig-yet")
    assert resp.status_code == 201, resp.text
