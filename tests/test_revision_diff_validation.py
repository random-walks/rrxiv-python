"""A stored CIR that no longer validates must not 500 the diff endpoint.

Regression guard for Sentry RRXIV-API-2: ``GET /papers/{id}/diff`` did an
unguarded ``CIR.model_validate`` on the *stored* CIRs, so an older corpus
version diffed against the evolved schema raised an uncaught
``ValidationError`` → 500 (Sentry noise). It now fails cleanly with a 422.
"""

from __future__ import annotations

from typing import Any

import pytest

from rrxiv.server import ServerSettings, build_app

pytest.importorskip("fastapi")


def _paper(pid: str, version: str, previous: str | None = None) -> dict[str, Any]:
    return {
        "rrxiv_version": "0.1.0",
        "id": pid,
        "version": version,
        "title": "T",
        "authors": [{"name": "A. Author"}],
        "abstract": "x",
        "submitted_at": "2026-05-04T00:00:00Z",
        "license": "CC-BY-4.0",
        "source": {"format": "latex", "uri": "https://x.org/p.tar.gz"},
        "previous_version": previous,
    }


def _client():  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    app = build_app(settings=ServerSettings(dev_mode=True))
    return app, TestClient(app)


def test_diff_with_invalid_stored_cir_returns_422_not_500() -> None:
    app, client = _client()
    store = app.state.store
    store.add_paper(_paper("p1", "v1"))
    store.add_paper(_paper("p2", "v2", previous="p1"))
    # p1's *stored* CIR is malformed (missing required CIR fields) —
    # exactly the RRXIV-API-2 situation. The diff endpoint must reject
    # cleanly, not raise an uncaught ValidationError (which 500s + spams
    # Sentry).
    store.add_cir({"id": "p1"})

    resp = client.get("/api/v0/papers/p2/diff?from=p1")
    assert resp.status_code == 422, resp.text
    assert "does not validate against the current schema" in resp.text
