"""Tests for the server-side render endpoints + real ORCID/hCaptcha
code paths (RRP-0006 follow-up; closes the v0.1 UX gaps)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from rrxiv.server import ServerSettings, build_app

pytest.importorskip("fastapi")


def _client(settings: ServerSettings) -> tuple[Any, httpx.Client]:
    """Return (app, sync httpx client) for an isolated reference server."""
    from fastapi.testclient import TestClient

    app = build_app(settings=settings)
    test_client = TestClient(app)
    sync = httpx.Client(
        transport=test_client._transport, base_url="http://testserver/api/v0"
    )
    return app, sync


# ----- ORCID render -----


def test_orcid_render_dev_mode_emits_paste_code() -> None:
    app, sync = _client(ServerSettings(dev_mode=True))
    with sync as c:
        resp = c.get(
            "/auth/orcid/render", params={"code": "dev-abc123", "state": "s"}
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "RRXIV-" in resp.text
    assert app.state.settings.orcid_dev_id in resp.text

    # The paste code should be persisted in the store.
    codes = list(app.state.store.state.paste_codes.values())
    assert len(codes) == 1
    assert codes[0].orcid_id == app.state.settings.orcid_dev_id
    assert not codes[0].consumed


def test_orcid_render_dev_mode_paste_redemption_round_trip() -> None:
    """Drive the full --no-browser flow without pre-seeding the store."""
    app, sync = _client(ServerSettings(dev_mode=True))
    with sync as c:
        # 1. User opens the render page (the rrxiv server's "callback").
        render = c.get(
            "/auth/orcid/render", params={"code": "dev-xyz", "state": "s"}
        )
        assert render.status_code == 200
        # Pull the paste code out of the rendered HTML.
        import re

        m = re.search(r"RRXIV-[A-F0-9]{4}-[A-F0-9]{4}", render.text)
        assert m is not None
        paste_code = m.group(0)

        # 2. CLI redeems it.
        resp = c.post(
            "/auth/orcid/exchange-paste", json={"code": paste_code}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["orcid_id"] == app.state.settings.orcid_dev_id


def test_orcid_render_real_mode_calls_orcid_token_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In non-dev mode the server posts to orcid.org/oauth/token.

    We mock httpx.post (used inside _resolve_orcid_id_from_code) so the
    test doesn't reach the network."""
    settings = ServerSettings(
        dev_mode=False,
        orcid_client_id="test-client",
        orcid_client_secret="test-secret",
        orcid_redirect_uri="https://rrxiv.com/auth/orcid/render",
    )
    captured: list[dict[str, Any]] = []

    def fake_post(url: str, *, data: Any = None, **kwargs: Any) -> Any:
        captured.append({"url": url, "data": dict(data or {})})

        class _Resp:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {
                    "access_token": "orcid-access-token",
                    "orcid": "0000-0001-9999-9999",
                    "token_type": "bearer",
                }

            text = ""

        return _Resp()

    monkeypatch.setattr("httpx.post", fake_post)

    _app, sync = _client(settings)
    with sync as c:
        resp = c.get(
            "/auth/orcid/render",
            params={"code": "real-orcid-code", "state": "s"},
        )
    assert resp.status_code == 200
    assert "0000-0001-9999-9999" in resp.text
    # Sanity: server hit the configured token endpoint with the
    # configured client_id.
    assert captured
    assert captured[0]["url"] == settings.orcid_token_url
    assert captured[0]["data"]["client_id"] == "test-client"
    assert captured[0]["data"]["code"] == "real-orcid-code"


def test_orcid_callback_real_mode_propagates_orcid_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If orcid.org rejects the code, the server returns 401."""
    settings = ServerSettings(
        dev_mode=False,
        orcid_client_id="test-client",
        orcid_client_secret="test-secret",
    )

    class _Resp:
        status_code = 401
        text = '{"error":"invalid_grant"}'

        def json(self) -> dict[str, Any]:
            return {"error": "invalid_grant"}

    monkeypatch.setattr(
        "httpx.post", lambda *a, **kw: _Resp()
    )

    _, sync = _client(settings)
    with sync as c:
        resp = c.post(
            "/auth/orcid/callback",
            json={"code": "bad", "state": "s"},
        )
    assert resp.status_code == 401


def test_orcid_callback_uses_redirect_uri_from_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The body's ``redirect_uri`` overrides the env-var default.

    Regression coverage: when the web client lives on a different
    origin than the static ``RRXIV_ORCID_REDIRECT_URI`` env var (e.g.
    Vercel 307s ``rrxiv.com`` → ``www.rrxiv.com``), the value sent in
    the authorize step won't match the env var. ORCID 401s the token
    exchange on the mismatch. Threading the redirect_uri through the
    callback body fixes this.
    """
    settings = ServerSettings(
        dev_mode=False,
        orcid_client_id="test-client",
        orcid_client_secret="test-secret",
        orcid_redirect_uri="https://example.invalid/should-not-be-used",
    )
    captured: list[dict[str, Any]] = []

    def fake_post(url: str, *, data: Any = None, **kwargs: Any) -> Any:
        captured.append({"url": url, "data": dict(data or {})})

        class _Resp:
            status_code = 200
            text = ""

            def json(self) -> dict[str, Any]:
                return {
                    "access_token": "x",
                    "orcid": "0000-0001-2345-6789",
                    "token_type": "bearer",
                }

        return _Resp()

    monkeypatch.setattr("httpx.post", fake_post)

    _, sync = _client(settings)
    with sync as c:
        resp = c.post(
            "/auth/orcid/callback",
            json={
                "code": "real-code",
                "state": "s",
                "redirect_uri": "https://www.rrxiv.com/api/auth/orcid/callback",
            },
        )
    assert resp.status_code == 200
    # Server used the body's value, NOT the env var default.
    assert captured[0]["data"]["redirect_uri"] == (
        "https://www.rrxiv.com/api/auth/orcid/callback"
    )


def test_orcid_callback_response_shape_has_identity_and_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Response carries ``identity``, ``identity_type``, ``display_name``,
    ``expires_in`` (canonical) AND ``orcid_id``, ``expires_in_seconds``
    (legacy aliases).

    The web client reads the canonical fields; the CLI reads the
    legacy ones. Both must work.
    """
    settings = ServerSettings(
        dev_mode=False,
        orcid_client_id="test-client",
        orcid_client_secret="test-secret",
        orcid_redirect_uri="https://rrxiv.com/api/auth/orcid/callback",
    )

    def fake_post(url: str, *, data: Any = None, **kwargs: Any) -> Any:
        class _Resp:
            status_code = 200
            text = ""

            def json(self) -> dict[str, Any]:
                return {
                    "access_token": "x",
                    "orcid": "0000-0001-2345-6789",
                    "name": "Alice Researcher",
                    "token_type": "bearer",
                }

        return _Resp()

    monkeypatch.setattr("httpx.post", fake_post)

    _, sync = _client(settings)
    with sync as c:
        resp = c.post(
            "/auth/orcid/callback",
            json={"code": "real", "state": "s"},
        )
    assert resp.status_code == 200
    body = resp.json()
    # Canonical web-facing fields
    assert body["identity"] == "0000-0001-2345-6789"
    assert body["identity_type"] == "orcid"
    assert body["display_name"] == "Alice Researcher"
    assert isinstance(body["expires_in"], int) and body["expires_in"] > 0
    # Legacy aliases (CLI still reads these)
    assert body["orcid_id"] == "0000-0001-2345-6789"
    assert body["expires_in_seconds"] == body["expires_in"]
    assert body["token"]


def test_orcid_callback_display_name_optional_when_orcid_omits_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ORCID doesn't always return ``name`` — author may have only an iD."""
    settings = ServerSettings(
        dev_mode=False,
        orcid_client_id="test-client",
        orcid_client_secret="test-secret",
    )

    def fake_post(url: str, *, data: Any = None, **kwargs: Any) -> Any:
        class _Resp:
            status_code = 200
            text = ""

            def json(self) -> dict[str, Any]:
                return {"orcid": "0000-0001-2345-6789"}  # no name

        return _Resp()

    monkeypatch.setattr("httpx.post", fake_post)

    _, sync = _client(settings)
    with sync as c:
        resp = c.post(
            "/auth/orcid/callback",
            json={"code": "real", "state": "s"},
        )
    assert resp.status_code == 200
    assert resp.json()["display_name"] is None


def test_orcid_callback_falls_back_to_env_redirect_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy callers without a ``redirect_uri`` in the body use the env var."""
    settings = ServerSettings(
        dev_mode=False,
        orcid_client_id="test-client",
        orcid_client_secret="test-secret",
        orcid_redirect_uri="https://rrxiv.com/api/auth/orcid/callback",
    )
    captured: list[dict[str, Any]] = []

    def fake_post(url: str, *, data: Any = None, **kwargs: Any) -> Any:
        captured.append({"data": dict(data or {})})

        class _Resp:
            status_code = 200
            text = ""

            def json(self) -> dict[str, Any]:
                return {"orcid": "0000-0001-1111-2222"}

        return _Resp()

    monkeypatch.setattr("httpx.post", fake_post)

    _, sync = _client(settings)
    with sync as c:
        resp = c.post(
            "/auth/orcid/callback",
            json={"code": "real", "state": "s"},  # no redirect_uri
        )
    assert resp.status_code == 200
    assert captured[0]["data"]["redirect_uri"] == (
        "https://rrxiv.com/api/auth/orcid/callback"
    )


def test_exchange_orcid_code_threads_loopback_redirect_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loopback flow (RFC 6749 §4.1.3): the redirect_uri the CLI
    authorized with — ``http://127.0.0.1:<port>/callback`` — is threaded
    through ``exchange_orcid_code`` into the ORCID token exchange, not
    the server's env-var default. Without this, ORCID 401s the exchange.
    """
    from fastapi.testclient import TestClient

    from rrxiv.auth import exchange_orcid_code

    settings = ServerSettings(
        dev_mode=False,
        orcid_client_id="test-client",
        orcid_client_secret="test-secret",
        orcid_redirect_uri="https://env.invalid/should-not-be-used",
    )
    app = build_app(settings=settings)
    transport = TestClient(app)._transport

    captured: list[dict[str, Any]] = []

    def fake_post(url: str, *, data: Any = None, **kwargs: Any) -> Any:
        captured.append({"url": url, "data": dict(data or {})})

        class _Resp:
            status_code = 200
            text = ""

            def json(self) -> dict[str, Any]:
                return {"orcid": "0000-0002-1111-2222"}

        return _Resp()

    monkeypatch.setattr("httpx.post", fake_post)

    loopback = "http://127.0.0.1:54321/callback"
    bearer = exchange_orcid_code(
        api_base="http://testserver/api/v0",
        code="real-loopback-code",
        state="s",
        expected_state="s",
        redirect_uri=loopback,
        transport=transport,
    )
    assert bearer.identity == "0000-0002-1111-2222"
    assert captured
    # The value sent to ORCID's token endpoint matches the authorize step.
    assert captured[0]["url"] == settings.orcid_token_url
    assert captured[0]["data"]["redirect_uri"] == loopback


def test_orcid_render_threads_its_own_url_as_redirect_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paste/render flow (RFC 6749 §4.1.3): ``/auth/orcid/render``
    exchanges the code using its OWN URL as the redirect_uri — the value
    the paste flow authorized with — not the server's env-var callback
    default. Without this, ORCID 401s the exchange."""
    settings = ServerSettings(
        dev_mode=False,
        orcid_client_id="test-client",
        orcid_client_secret="test-secret",
        orcid_redirect_uri="https://env.invalid/callback",
    )
    captured: list[dict[str, Any]] = []

    def fake_post(url: str, *, data: Any = None, **kwargs: Any) -> Any:
        captured.append({"url": url, "data": dict(data or {})})

        class _Resp:
            status_code = 200
            text = ""

            def json(self) -> dict[str, Any]:
                return {"orcid": "0000-0003-4444-5555"}

        return _Resp()

    monkeypatch.setattr("httpx.post", fake_post)

    _app, sync = _client(settings)
    with sync as c:
        resp = c.get(
            "/auth/orcid/render",
            params={"code": "real-render-code", "state": "s"},
        )
    assert resp.status_code == 200, resp.text
    assert captured
    # The render endpoint authorized with (and exchanges with) its own
    # URL, minus the ORCID-appended ?code=…&state=… query.
    assert captured[0]["data"]["redirect_uri"] == (
        "http://testserver/api/v0/auth/orcid/render"
    )


# ----- Anonymous render -----


def test_anonymous_render_returns_html_with_widget() -> None:
    _app, sync = _client(ServerSettings(dev_mode=True))
    with sync as c:
        # Mint a real challenge first.
        challenge_resp = c.post("/auth/anonymous/challenge")
        assert challenge_resp.status_code == 200
        cid = challenge_resp.json()["challenge_id"]
        site_key = challenge_resp.json()["site_key"]
        # Render
        resp = c.get(
            "/auth/anonymous/render",
            params={"challenge_id": cid, "site_key": site_key},
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "h-captcha" in resp.text
    assert site_key in resp.text
    assert cid in resp.text


def test_anonymous_render_404_for_unknown_challenge() -> None:
    _, sync = _client(ServerSettings(dev_mode=True))
    with sync as c:
        resp = c.get(
            "/auth/anonymous/render",
            params={"challenge_id": "never-issued", "site_key": "x"},
        )
    assert resp.status_code == 404


def test_anonymous_verify_real_mode_calls_hcaptcha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In non-dev mode the server posts to api.hcaptcha.com/siteverify."""
    settings = ServerSettings(dev_mode=False, hcaptcha_secret="testsecret")
    captured: list[dict[str, Any]] = []

    def fake_post(url: str, *, data: Any = None, **kwargs: Any) -> Any:
        captured.append({"url": url, "data": dict(data or {})})

        class _Resp:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {"success": True, "challenge_ts": "now", "hostname": "x"}

            text = ""

        return _Resp()

    monkeypatch.setattr("httpx.post", fake_post)

    _app, sync = _client(settings)
    with sync as c:
        challenge = c.post("/auth/anonymous/challenge").json()
        verify = c.post(
            "/auth/anonymous/verify",
            json={
                "challenge_id": challenge["challenge_id"],
                "response": "h-captcha-response-token",
            },
        )
    assert verify.status_code == 200
    assert captured
    assert captured[0]["url"] == "https://api.hcaptcha.com/siteverify"
    assert captured[0]["data"]["secret"] == "testsecret"
    assert captured[0]["data"]["response"] == "h-captcha-response-token"


def test_anonymous_verify_real_mode_rejects_failed_hcaptcha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = ServerSettings(dev_mode=False, hcaptcha_secret="testsecret")

    class _Resp:
        status_code = 200
        text = ""

        def json(self) -> dict[str, Any]:
            return {"success": False, "error-codes": ["invalid-input-response"]}

    monkeypatch.setattr("httpx.post", lambda *a, **kw: _Resp())

    _, sync = _client(settings)
    with sync as c:
        challenge = c.post("/auth/anonymous/challenge").json()
        verify = c.post(
            "/auth/anonymous/verify",
            json={
                "challenge_id": challenge["challenge_id"],
                "response": "anything",
            },
        )
    assert verify.status_code == 401
