"""Tests for refresh tokens (RRP-0009) and agent key rotation
(RRP-0010)."""

from __future__ import annotations

import base64
import time
from typing import Any

import httpx
import pytest

from rrxiv.auth import (
    AgentEnrollmentRequest,
    enroll_agent,
    exchange_orcid_code,
    refresh_bearer,
)
from rrxiv.auth.agent import (
    build_enrollment_payload,
    rotate_agent_key,
    sign_enrollment_payload,
)
from rrxiv.client import AgentSigningKey, RrxivClient
from rrxiv.client.errors import (
    ForbiddenError,
    UnauthorizedError,
)
from rrxiv.server import ServerSettings, build_app

pytest.importorskip("fastapi")
pytest.importorskip("cryptography")


def _client():  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    app = build_app(settings=ServerSettings(dev_mode=True))
    test_client = TestClient(app)
    return app, test_client._transport


def _orcid_bearer(transport: httpx.BaseTransport) -> Any:
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


# ----- Refresh -----


def test_refresh_round_trip_orcid_bearer() -> None:
    app, transport = _client()
    bearer = _orcid_bearer(transport)
    refreshed = refresh_bearer(
        api_base="http://test/api/v0",
        current=bearer,
        transport=transport,
    )
    assert refreshed.token.token != bearer.token
    assert refreshed.token.identity_type == "orcid"
    assert refreshed.token.identity == bearer.identity
    assert refreshed.expires_in_seconds > 0
    # Old token is revoked.
    assert app.state.store.get_token(bearer.token) is None
    # New token works.
    assert app.state.store.get_token(refreshed.token.token) is not None


def test_refresh_anonymous_forbidden() -> None:
    from rrxiv.auth import (
        AnonymousChallengeResponse,
        request_anonymous_challenge,
        verify_anonymous_challenge,
    )

    _, transport = _client()
    challenge = request_anonymous_challenge(
        api_base="http://test/api/v0", transport=transport
    )
    bearer = verify_anonymous_challenge(
        api_base="http://test/api/v0",
        response=AnonymousChallengeResponse(
            challenge_id=challenge.challenge_id, response="x"
        ),
        transport=transport,
    )
    with pytest.raises(ForbiddenError):
        refresh_bearer(
            api_base="http://test/api/v0",
            current=bearer,
            transport=transport,
        )


def test_refresh_unknown_token_returns_401() -> None:
    _, transport = _client()
    fake = type(
        "Bearer",
        (),
        {
            "token": "made-up-token",
            "identity_type": "orcid",
            "identity": "x",
        },
    )()
    with pytest.raises(UnauthorizedError):
        refresh_bearer(
            api_base="http://test/api/v0",
            current=fake,  # type: ignore[arg-type]
            transport=transport,
        )


# ----- Agent key rotation -----


def _enroll_agent_for_test(transport: httpx.BaseTransport) -> tuple[Any, AgentSigningKey]:
    """Enroll @rotate-bot, return (BearerToken, signing key)."""
    signing = AgentSigningKey.generate(handle="@rotate-bot")
    pub_b64 = base64.standard_b64encode(signing.public_key_bytes()).decode("ascii")
    payload = build_enrollment_payload(
        handle=signing.handle,
        public_key_b64=pub_b64,
        issued_at=int(time.time()),
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
    return bearer, signing


def test_rotate_agent_key_round_trip() -> None:
    app, transport = _client()
    bearer, old_signing = _enroll_agent_for_test(transport)

    # Generate the new keypair.
    new_signing = AgentSigningKey.generate(handle="@rotate-bot")
    new_priv_bytes = new_signing.private_key_bytes()

    rotated = rotate_agent_key(
        api_base="http://test/api/v0",
        handle="@rotate-bot",
        bearer=bearer,
        old_signing_key=old_signing,
        new_private_key_bytes=new_priv_bytes,
        transport=transport,
    )
    assert rotated.handle == "@rotate-bot"
    new_pub_b64 = base64.standard_b64encode(
        new_signing.public_key_bytes()
    ).decode("ascii")
    assert rotated.public_key_b64 == new_pub_b64

    # Server has the new public key now.
    assert app.state.store.state.agents["@rotate-bot"].public_key_b64 == new_pub_b64

    # A signed write with the *old* key should now fail (signature
    # verification uses the new public key).
    app.state.store.add_paper(
        {
            "rrxiv_version": "0.1.0",
            "id": "p-rotate",
            "version": "v1",
            "title": "T",
            "authors": [{"name": "A"}],
            "abstract": "x",
            "submitted_at": "2026-05-04T00:00:00Z",
            "license": "CC-BY-4.0",
            "source": {"format": "latex", "uri": "https://x.org/p.tar.gz"},
        }
    )
    with RrxivClient(
        "http://test/api/v0",
        transport=transport,
        auth=bearer,
        agent_signing_key=old_signing,  # the *old* key, no longer registered
    ) as client:
        with pytest.raises(UnauthorizedError):
            client.create_annotation(
                {
                    "id": "ann-rotate-fail",
                    "target_id": "p-rotate",
                    "target_type": "paper",
                    "annotation_type": "comment",
                    "content": "should fail",
                    "created_at": "2026-05-06T00:00:00Z",
                    "created_by": {
                        "identity_type": "agent",
                        "identity": "@rotate-bot",
                    },
                }
            )

    # And a signed write with the new key should succeed.
    with RrxivClient(
        "http://test/api/v0",
        transport=transport,
        auth=bearer,
        agent_signing_key=new_signing,
    ) as client:
        ann = client.create_annotation(
            {
                "id": "ann-rotate-ok",
                "target_id": "p-rotate",
                "target_type": "paper",
                "annotation_type": "comment",
                "content": "should succeed",
                "created_at": "2026-05-06T00:00:00Z",
                "created_by": {
                    "identity_type": "agent",
                    "identity": "@rotate-bot",
                },
            }
        )
    assert ann.id == "ann-rotate-ok"


def test_rotate_agent_key_handle_mismatch_forbidden() -> None:
    _app, transport = _client()
    bearer, signing = _enroll_agent_for_test(transport)

    # Try to rotate the key for a *different* handle than our bearer.
    new_priv = AgentSigningKey.generate(handle="@rotate-bot").private_key_bytes()
    with pytest.raises(ForbiddenError):
        rotate_agent_key(
            api_base="http://test/api/v0",
            handle="@some-other-bot",  # not our bearer's identity
            bearer=bearer,
            old_signing_key=signing,
            new_private_key_bytes=new_priv,
            transport=transport,
        )
