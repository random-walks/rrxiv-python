"""Cross-tests: drive RrxivClient against the FastAPI reference server.

These prove that the client and server agree on every wire-format
detail. Each test exercises a real ASGI dispatch (not the mock).
"""

from __future__ import annotations

import base64
import time
from typing import Any

import httpx
import pytest

from rrxiv.auth import (
    AgentEnrollmentRequest,
    AnonymousChallengeResponse,
    enroll_agent,
    exchange_orcid_code,
    request_anonymous_challenge,
    verify_anonymous_challenge,
)
from rrxiv.auth.agent import build_enrollment_payload, sign_enrollment_payload
from rrxiv.client import (
    AgentSigningKey,
    ForbiddenError,
    NotFoundError,
    RrxivClient,
    UnauthorizedError,
)
from rrxiv.server import ServerSettings, build_app

pytest.importorskip("fastapi")
pytest.importorskip("cryptography")


def _paper(paper_id: str = "p1") -> dict[str, Any]:
    return {
        "rrxiv_version": "0.1.0",
        "id": paper_id,
        "version": "v1",
        "title": "T",
        "authors": [{"name": "A. Author"}],
        "abstract": "x",
        "submitted_at": "2026-05-04T00:00:00Z",
        "license": "CC-BY-4.0",
        "source": {"format": "latex", "uri": "https://x.org/p.tar.gz"},
    }


def _build_app_and_transport():  # type: ignore[no-untyped-def]
    """Build the app + a sync-friendly transport.

    httpx.ASGITransport is async-only. We borrow Starlette's TestClient
    transport (an httpx-shaped sync wrapper around the ASGI app) so
    sync RrxivClient and the auth helper functions can drive it.
    """
    from fastapi.testclient import TestClient

    settings = ServerSettings(dev_mode=True)
    app = build_app(settings=settings)
    test_client = TestClient(app)
    return app, test_client._transport


# -------------------- Basic reads --------------------


def test_version_endpoint() -> None:
    _app, transport = _build_app_and_transport()
    with httpx.Client(
        transport=transport, base_url="http://test/api/v0"
    ) as c:
        resp = c.get("/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["protocol"] == "0.1.0"
    assert "v0" in body["supported_api_versions"]


def test_paper_round_trip_via_client() -> None:
    app, transport = _build_app_and_transport()
    app.state.store.add_paper(_paper("p1"))
    with RrxivClient(
        "http://test/api/v0", transport=transport
    ) as client:
        paper = client.get_paper("p1")
        assert paper.id == "p1"
        page = client.list_papers()
    assert any(p["id"] == "p1" for p in page["items"])


def test_404_for_unknown_paper() -> None:
    _, transport = _build_app_and_transport()
    with RrxivClient(
        "http://test/api/v0", transport=transport
    ) as client:
        with pytest.raises(NotFoundError):
            client.get_paper("nope")


# -------------------- ORCID OAuth (dev mode) --------------------


def test_orcid_dev_mode_round_trip() -> None:
    app, transport = _build_app_and_transport()
    # Step 1: hit /auth/orcid/start in dev mode → 302 to dev redirect_uri
    with httpx.Client(
        transport=transport, base_url="http://test/api/v0"
    ) as c:
        resp = c.get(
            "/auth/orcid/start",
            params={
                "redirect_uri": "http://127.0.0.1:9999/cb",
                "state": "test-state",
            },
            follow_redirects=False,
        )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("http://127.0.0.1:9999/cb?code=dev-")
    code = location.split("code=", 1)[1].split("&")[0]
    state = location.split("state=", 1)[1]
    assert state == "test-state"

    # Step 2: client exchanges code via the auth helper.
    token = exchange_orcid_code(
        api_base="http://test/api/v0",
        code=code,
        state=state,
        expected_state=state,
        transport=transport,
    )
    assert token.identity_type == "orcid"
    assert token.identity == app.state.settings.orcid_dev_id


# -------------------- Agent enrollment + signed write --------------------


def test_agent_enroll_and_signed_annotation_create() -> None:
    app, transport = _build_app_and_transport()
    app.state.store.add_paper(_paper("p1"))

    # Generate keypair locally.
    signing = AgentSigningKey.generate(handle="@cross-test-bot")
    pub_b64 = base64.standard_b64encode(signing.public_key_bytes()).decode("ascii")
    payload = build_enrollment_payload(
        handle=signing.handle,
        public_key_b64=pub_b64,
        issued_at=int(time.time()),
    )
    sig_b64 = sign_enrollment_payload(
        payload=payload, private_key_bytes=signing.private_key_bytes()
    )
    request = AgentEnrollmentRequest(
        handle=signing.handle,
        public_key_b64=pub_b64,
        payload_b64=base64.standard_b64encode(payload).decode("ascii"),
        signature_b64=sig_b64,
        contact="ops@example.com",
    )
    bearer = enroll_agent(
        api_base="http://test/api/v0",
        request=request,
        transport=transport,
    )
    assert bearer.identity == "@cross-test-bot"

    # Now use both bearer + signing key to create an annotation.
    with RrxivClient(
        "http://test/api/v0",
        transport=transport,
        auth=bearer,
        agent_signing_key=signing,
    ) as client:
        ann = client.create_annotation(
            {
                "id": "ann-cross-1",
                "target_id": "p1",
                "target_type": "paper",
                "annotation_type": "comment",
                "content": "hello from a signed agent",
                "created_at": "2026-05-06T00:00:00Z",
                "created_by": {
                    "identity_type": "agent",
                    "identity": "@cross-test-bot",
                },
            }
        )
    assert ann.id == "ann-cross-1"
    assert "ann-cross-1" in app.state.store.state.annotations


def test_agent_write_without_signature_fails_401() -> None:
    """Bearer alone is not enough for an agent identity write."""
    _app, transport = _build_app_and_transport()

    signing = AgentSigningKey.generate(handle="@no-sig-bot")
    pub_b64 = base64.standard_b64encode(signing.public_key_bytes()).decode("ascii")
    payload = build_enrollment_payload(
        handle=signing.handle, public_key_b64=pub_b64, issued_at=int(time.time())
    )
    sig_b64 = sign_enrollment_payload(
        payload=payload, private_key_bytes=signing.private_key_bytes()
    )
    bearer = enroll_agent(
        api_base="http://test/api/v0",
        request=AgentEnrollmentRequest(
            handle=signing.handle,
            public_key_b64=pub_b64,
            payload_b64=base64.standard_b64encode(payload).decode("ascii"),
            signature_b64=sig_b64,
        ),
        transport=transport,
    )

    # Note: agent_signing_key is NOT set on the client.
    with RrxivClient(
        "http://test/api/v0",
        transport=transport,
        auth=bearer,
    ) as client:
        with pytest.raises(UnauthorizedError, match="signature"):
            client.create_annotation(
                {
                    "id": "ann-x",
                    "target_id": "p1",
                    "target_type": "paper",
                    "annotation_type": "comment",
                    "content": ".",
                    "created_at": "2026-05-06T00:00:00Z",
                    "created_by": {
                        "identity_type": "agent",
                        "identity": "@no-sig-bot",
                    },
                }
            )


def test_orcid_write_does_not_require_signature() -> None:
    """ORCID identities use bearer-only for writes."""
    app, transport = _build_app_and_transport()

    # Mint an ORCID token via the dev flow.
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        resp = c.get(
            "/auth/orcid/start",
            params={"redirect_uri": "http://x/cb", "state": "s"},
            follow_redirects=False,
        )
    code = resp.headers["location"].split("code=", 1)[1].split("&")[0]
    bearer = exchange_orcid_code(
        api_base="http://test/api/v0",
        code=code,
        state="s",
        expected_state="s",
        transport=transport,
    )

    with RrxivClient(
        "http://test/api/v0", transport=transport, auth=bearer
    ) as client:
        ann = client.create_annotation(
            {
                "id": "ann-orcid-1",
                "target_id": "p1",
                "target_type": "paper",
                "annotation_type": "comment",
                "content": ".",
                "created_at": "2026-05-06T00:00:00Z",
                "created_by": {
                    "identity_type": "orcid",
                    "identity": app.state.settings.orcid_dev_id,
                },
            }
        )
    assert ann.id == "ann-orcid-1"


# -------------------- Anonymous --------------------


def test_anonymous_challenge_then_verify_round_trip() -> None:
    _app, transport = _build_app_and_transport()
    challenge = request_anonymous_challenge(
        api_base="http://test/api/v0", transport=transport
    )
    assert challenge.challenge_type == "hcaptcha"
    bearer = verify_anonymous_challenge(
        api_base="http://test/api/v0",
        response=AnonymousChallengeResponse(
            challenge_id=challenge.challenge_id,
            response="dev-mode-accepts-anything",
        ),
        transport=transport,
    )
    assert bearer.identity_type == "anonymous"


def test_anonymous_cannot_create_annotation() -> None:
    _app, transport = _build_app_and_transport()
    challenge = request_anonymous_challenge(
        api_base="http://test/api/v0", transport=transport
    )
    bearer = verify_anonymous_challenge(
        api_base="http://test/api/v0",
        response=AnonymousChallengeResponse(
            challenge_id=challenge.challenge_id,
            response="ok",
        ),
        transport=transport,
    )
    with RrxivClient(
        "http://test/api/v0", transport=transport, auth=bearer
    ) as client:
        with pytest.raises(ForbiddenError):
            client.create_annotation(
                {
                    "id": "ann-anon",
                    "target_id": "p1",
                    "target_type": "paper",
                    "annotation_type": "comment",
                    "content": ".",
                    "created_at": "2026-05-06T00:00:00Z",
                    "created_by": {
                        "identity_type": "anonymous",
                        "identity": "x",
                    },
                }
            )


# -------------------- Idempotency --------------------


def test_idempotent_replay_returns_same_response() -> None:
    """POST /annotations with same Idempotency-Key + same body returns
    the cached response."""
    app, transport = _build_app_and_transport()

    # ORCID identity (bearer-only) for simplicity.
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        resp = c.get(
            "/auth/orcid/start",
            params={"redirect_uri": "http://x/cb", "state": "s"},
            follow_redirects=False,
        )
    code = resp.headers["location"].split("code=", 1)[1].split("&")[0]
    bearer = exchange_orcid_code(
        api_base="http://test/api/v0",
        code=code,
        state="s",
        expected_state="s",
        transport=transport,
    )

    body = {
        "id": "ann-idem",
        "target_id": "p1",
        "target_type": "paper",
        "annotation_type": "comment",
        "content": "first",
        "created_at": "2026-05-06T00:00:00Z",
        "created_by": {
            "identity_type": "orcid",
            "identity": app.state.settings.orcid_dev_id,
        },
    }
    with RrxivClient(
        "http://test/api/v0", transport=transport, auth=bearer
    ) as client:
        ann1 = client.create_annotation(dict(body), idempotency_key="key-1")
        ann2 = client.create_annotation(dict(body), idempotency_key="key-1")
    assert ann1.id == ann2.id == "ann-idem"
