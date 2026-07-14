"""End-to-end tests for ``rrxiv login`` subcommands.

These run the typer apps via :class:`typer.testing.CliRunner` against
the in-memory FastAPI reference server. The OAuth flow is tested with
the loopback listener mocked out.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from rrxiv.cli.app import app
from rrxiv.cli.credentials import load_agent_key, load_bearer

pytest.importorskip("fastapi")
pytest.importorskip("cryptography")
pytest.importorskip("uvicorn")


@pytest.fixture()
def cred_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Force file-based credential storage in a tmpdir."""
    monkeypatch.setenv("RRXIV_CRED_BACKEND", "file")
    monkeypatch.setenv("RRXIV_CRED_DIR", str(tmp_path / "rrxiv"))
    return tmp_path / "rrxiv"


@pytest.fixture()
def reference_server() -> Any:
    """Spin up a real uvicorn server on an ephemeral port for the test
    duration. Background-threaded so the CLI's OAuth listener doesn't
    deadlock against the same event loop."""
    import socket

    import uvicorn

    from rrxiv.server import ServerSettings, build_app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    app_ = build_app(settings=ServerSettings(dev_mode=True))
    config = uvicorn.Config(
        app_, host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # Wait until the server is accepting connections.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=0.5) as c:
                if c.get(f"http://127.0.0.1:{port}/api/v0/version").status_code == 200:
                    break
        except (httpx.ConnectError, httpx.ReadTimeout):
            time.sleep(0.05)
    else:  # pragma: no cover
        pytest.fail("reference server failed to start")

    yield {"app": app_, "url": f"http://127.0.0.1:{port}/api/v0", "port": port}

    server.should_exit = True
    thread.join(timeout=5)


def test_login_agent_end_to_end(
    cred_env: Path, reference_server: Any
) -> None:
    """Run `rrxiv login agent --handle @e2e-bot` against the live
    reference server. Persisted bearer + private key should be loadable."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "login",
            "agent",
            "--server",
            reference_server["url"],
            "--handle",
            "@e2e-bot",
            "--contact",
            "ops@example.com",
        ],
    )
    assert result.exit_code == 0, result.output

    bearer = load_bearer(reference_server["url"], "agent")
    assert bearer is not None
    assert bearer.identity == "@e2e-bot"

    agent_key = load_agent_key(reference_server["url"], "@e2e-bot")
    assert agent_key is not None
    assert len(agent_key.private_key_bytes()) == 32


def test_login_status_lists_logged_in_identity(
    cred_env: Path, reference_server: Any
) -> None:
    runner = CliRunner()
    runner.invoke(
        app,
        [
            "login",
            "agent",
            "--server",
            reference_server["url"],
            "--handle",
            "@status-bot",
        ],
    )
    result = runner.invoke(
        app, ["login", "status", "--server", reference_server["url"]]
    )
    assert result.exit_code == 0
    assert "@status-bot" in result.output
    assert reference_server["url"] in result.output


def test_logout_clears_credentials(
    cred_env: Path, reference_server: Any
) -> None:
    runner = CliRunner()
    runner.invoke(
        app,
        [
            "login",
            "agent",
            "--server",
            reference_server["url"],
            "--handle",
            "@logout-bot",
        ],
    )
    assert load_bearer(reference_server["url"], "agent") is not None

    result = runner.invoke(
        app, ["logout", "--server", reference_server["url"]]
    )
    assert result.exit_code == 0
    assert load_bearer(reference_server["url"], "agent") is None
    assert load_agent_key(reference_server["url"], "@logout-bot") is None


def test_login_anonymous_paste_flow(
    cred_env: Path, reference_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anonymous flow uses a paste-back; supply the response via stdin."""
    runner = CliRunner()
    # First fetch a challenge via the API directly so we have a valid
    # challenge_id to "solve".
    with httpx.Client() as c:
        c.post(f"{reference_server['url']}/auth/anonymous/challenge")

    # Re-run the actual CLI which issues its own challenge then prompts.
    result = runner.invoke(
        app,
        ["login", "anonymous", "--server", reference_server["url"]],
        input="solved-token\n",
    )
    assert result.exit_code == 0, result.output
    bearer = load_bearer(reference_server["url"], "anonymous")
    assert bearer is not None


def test_login_orcid_paste_fallback(
    cred_env: Path, reference_server: Any
) -> None:
    """``--no-browser`` paste flow — drives the real /auth/orcid/render
    endpoint to mint a paste code."""
    import re

    # 1. Hit the render endpoint as a "browser" would after ORCID
    # OAuth. In dev mode the server accepts a dev-prefixed code.
    with httpx.Client() as c:
        render = c.get(
            f"{reference_server['url']}/auth/orcid/render",
            params={"code": "dev-paste-fallback", "state": "x"},
        )
    assert render.status_code == 200, render.text
    m = re.search(r"RRXIV-[A-F0-9]{4}-[A-F0-9]{4}", render.text)
    assert m is not None
    paste_code = m.group(0)

    # 2. CLI redeems it.
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "login",
            "orcid",
            "--server",
            reference_server["url"],
            "--no-browser",
        ],
        input=f"{paste_code}\n",
    )
    assert result.exit_code == 0, result.output
    bearer = load_bearer(reference_server["url"], "orcid")
    assert bearer is not None
    # Dev mode returns the configured dev iD.
    assert bearer.identity == reference_server["app"].state.settings.orcid_dev_id


def test_login_orcid_loopback_simulated(
    cred_env: Path,
    reference_server: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loopback flow against the live dev-mode server.

    The dev server's ``/auth/orcid/start`` immediately 302s back to the
    listener with a ``code=dev-…``. We replace ``webbrowser.open`` with
    a function that hits the URL itself, simulating the user-completes-
    OAuth-in-browser step.
    """
    import httpx as _httpx

    def fake_open(url: str, *args: object, **kwargs: object) -> bool:
        # Follow the redirect ourselves to drive the loopback.
        with _httpx.Client(follow_redirects=False) as client:
            resp = client.get(url)
            assert resp.status_code == 302
            redirect = resp.headers["location"]
            client.get(redirect)  # 200 from the CLI's own listener
        return True

    monkeypatch.setattr("rrxiv.cli.login.webbrowser.open", fake_open)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "login",
            "orcid",
            "--server",
            reference_server["url"],
            "--timeout",
            "10",
        ],
    )
    assert result.exit_code == 0, result.output
    bearer = load_bearer(reference_server["url"], "orcid")
    assert bearer is not None
    assert bearer.identity_type == "orcid"


# ----------------------- agent-drivable ORCID login -----------------------


def test_login_orcid_print_url(cred_env: Path, reference_server: Any) -> None:
    """--print-url emits the paste-flow authorization URL and exits 0."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["login", "orcid", "--server", reference_server["url"], "--print-url"],
    )
    assert result.exit_code == 0, result.output
    url = result.output.strip()
    assert "/auth/orcid/start?" in url
    assert "redirect_uri=" in url
    assert "render" in url
    # No token was stored — this is half 1 only.
    assert load_bearer(reference_server["url"], "orcid") is None


def test_login_orcid_code_redeems_and_persists(
    cred_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--code posts to exchange-paste and persists the bearer."""
    import httpx as _httpx

    captured: dict[str, Any] = {}

    def fake_post(url: str, json: Any = None, timeout: float = 0) -> Any:
        captured["url"] = url
        captured["json"] = json
        return _httpx.Response(
            200,
            json={
                "token": "tok-123",
                "orcid_id": "0000-0000-0000-0000",
                "expires_in_seconds": 3600,
            },
            request=_httpx.Request("POST", url),
        )

    monkeypatch.setattr("httpx.post", fake_post)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "login",
            "orcid",
            "--server",
            "https://api.example.com/api/v0",
            "--code",
            "RRXIV-TEST-CODE",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["url"].endswith("/auth/orcid/exchange-paste")
    assert captured["json"] == {"code": "RRXIV-TEST-CODE"}
    bearer = load_bearer("https://api.example.com/api/v0", "orcid")
    assert bearer is not None
    assert bearer.identity == "0000-0000-0000-0000"
    assert bearer.token == "tok-123"


def test_login_orcid_code_rejected(
    cred_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected paste code exits non-zero and stores nothing."""
    import httpx as _httpx

    def fake_post(url: str, json: Any = None, timeout: float = 0) -> Any:
        return _httpx.Response(
            400, json={"detail": "bad code"}, request=_httpx.Request("POST", url)
        )

    monkeypatch.setattr("httpx.post", fake_post)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "login",
            "orcid",
            "--server",
            "https://api.example.com/api/v0",
            "--code",
            "RRXIV-BAD-CODE",
        ],
    )
    assert result.exit_code == 2
    assert load_bearer("https://api.example.com/api/v0", "orcid") is None


def test_login_orcid_remote_defaults_to_paste_flow(
    cred_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Against a non-loopback server the paste flow is the default —
    the local-listener flow only works for dev ORCID apps registered
    with localhost redirect URIs."""
    from rrxiv.cli import login as login_mod

    called: dict[str, str] = {}
    monkeypatch.setattr(
        login_mod,
        "_login_orcid_paste",
        lambda api_base: called.setdefault("api_base", api_base),
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["login", "orcid", "--server", "https://api.example.com/api/v0"],
    )
    assert result.exit_code == 0, result.output
    assert called["api_base"] == "https://api.example.com/api/v0"
    assert "paste-back flow" in result.output
