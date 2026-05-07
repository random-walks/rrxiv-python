"""Pytest fixture spinning up a real uvicorn-backed reference server.

Importable by downstream client packages (e.g., `rrxiv-go-tester` or
similar) that want to drive a real HTTP loop in their integration
tests instead of the in-process MockTransport.

Usage::

    from rrxiv.testing import live_server

    def test_my_thing(live_server):
        url = live_server.url
        # ... drive the server ...

The fixture binds an OS-assigned ephemeral port, runs the FastAPI
reference server in a daemon thread, waits until /version returns
200, and yields a small handle. Teardown signals shutdown to uvicorn.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import pytest

if TYPE_CHECKING:
    from rrxiv.server.settings import ServerSettings


@dataclass(frozen=True, slots=True)
class LiveServer:
    """Handle returned by the :func:`live_server` fixture."""

    url: str
    """API base URL, e.g. ``http://127.0.0.1:54321/api/v0``."""

    port: int
    app: Any
    """The FastAPI app, useful for poking at app.state.store."""


@pytest.fixture()
def live_server(
    request: pytest.FixtureRequest,
) -> Any:
    """Start a uvicorn-backed reference server bound to 127.0.0.1.

    The fixture builds a fresh server with default settings (dev
    mode on). To override, parametrise via
    ``@pytest.mark.parametrize("live_server_settings", [...], indirect=True)``
    in your test module — but for v0.1 most callers want defaults.
    """
    try:
        import uvicorn

        from rrxiv.server import build_app
        from rrxiv.server.settings import ServerSettings
    except ImportError as e:
        pytest.skip(
            f"live_server fixture needs the [server] extra: {e}"
        )

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    settings: ServerSettings = ServerSettings(dev_mode=True)
    app = build_app(settings=settings)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}/api/v0"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=0.5) as c:
                if c.get(f"{base_url}/version").status_code == 200:
                    break
        except (httpx.ConnectError, httpx.ReadTimeout):
            time.sleep(0.05)
    else:  # pragma: no cover
        pytest.fail("live_server failed to start")

    handle = LiveServer(url=base_url, port=port, app=app)
    yield handle

    server.should_exit = True
    thread.join(timeout=5)


__all__ = ["LiveServer", "live_server"]
