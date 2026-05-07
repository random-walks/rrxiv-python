"""Smoke-test the rrxiv.testing.live_server pytest fixture (Phase 5)."""

from __future__ import annotations

import httpx
import pytest

# Tell pytest where to find the fixture for this module.
pytest_plugins = ["rrxiv.testing.live_server"]


pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")


def test_live_server_fixture_is_reachable(live_server) -> None:  # type: ignore[no-untyped-def]
    """The fixture starts a server and `/version` returns 200."""
    with httpx.Client() as c:
        resp = c.get(f"{live_server.url}/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["protocol"] == "0.1.0"


def test_live_server_handle_exposes_app(live_server) -> None:  # type: ignore[no-untyped-def]
    """live_server.app is the underlying FastAPI app — useful for
    poking at the in-memory store."""
    assert live_server.app is not None
    assert live_server.app.state.store is not None
