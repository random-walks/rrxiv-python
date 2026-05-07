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
    """``--no-browser`` paste flow.

    We pre-seed the server's paste_codes table (the render endpoint is
    out of scope for v0.1; paste codes only come from a future render
    endpoint). This test exercises the CLI side of the paste flow.
    """
    from rrxiv.server.store.protocol import PasteCodeEntry

    app_ = reference_server["app"]
    app_.state.store.add_paste_code(
        PasteCodeEntry(
            code="TEST-PASTE-CODE",
            orcid_id="0000-0001-2345-6789",
            issued_at_unix=int(time.time()),
            expires_at_unix=int(time.time()) + 300,
        )
    )

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
        input="TEST-PASTE-CODE\n",
    )
    assert result.exit_code == 0, result.output
    bearer = load_bearer(reference_server["url"], "orcid")
    assert bearer is not None
    assert bearer.identity == "0000-0001-2345-6789"


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
