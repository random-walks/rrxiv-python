"""Tests for the RRP-0028 authors router extensions.

Verifies:
- /authors/{orcid} resolves human profiles with identity_type=human.
- /authors/{agent_handle} resolves agent profiles with identity_type=agent.
- The agent profile carries provenance.models[] aggregation.
- /authors/{ident}/papers and /authors/{ident}/claims paginate correctly.
- co_authors is populated from cross-paper participation.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from rrxiv.server import ServerSettings, build_app

pytest.importorskip("fastapi")


def _seed(store) -> None:
    paper = {
        "rrxiv_version": "0.1.0",
        "id": "paper-1",
        "id_slug": "rrxiv:2605.00001",
        "version": "v1",
        "title": "First paper",
        "authors": [
            {
                "name": "Blaise Albis-Burdige",
                "orcid": "0009-0002-0561-6499",
                "is_agent": False,
                "role": "author",
            },
            {
                "name": "Claude Opus 4.7",
                "is_agent": True,
                "agent_handle": "agent:claude-opus-4.7",
                "role": "agent",
                "provenance": {
                    "models": [
                        {
                            "name": "Claude Opus 4.7",
                            "vendor": "anthropic",
                            "family": "claude",
                            "release_pin": "claude-opus-4-7-20260520",
                        }
                    ]
                },
            },
        ],
        "abstract": "test",
        "submitted_at": "2026-05-26T18:00:00Z",
        "license": "CC-BY-4.0",
        "source": {"format": "latex", "uri": "/x.tar.gz"},
    }
    store.add_paper(paper)
    cir = dict(paper)
    cir["claims"] = []
    store.add_cir(cir)


def _client():  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    app = build_app(settings=ServerSettings(dev_mode=True))
    test_client = TestClient(app)
    return app, test_client._transport


def _get(transport: httpx.BaseTransport, path: str, **params: Any) -> dict[str, Any]:
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        resp = c.get(path, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_human_profile_identity_type() -> None:
    app, transport = _client()
    _seed(app.state.store)
    profile = _get(transport, "/authors/0009-0002-0561-6499")
    assert profile["identity_type"] == "human"
    assert profile["name"] == "Blaise Albis-Burdige"
    assert profile["orcid"] == "0009-0002-0561-6499"
    assert profile["agent_handle"] is None
    assert profile["is_agent"] is False
    assert profile["paper_count"] == 1


def test_agent_profile_identity_type() -> None:
    app, transport = _client()
    _seed(app.state.store)
    profile = _get(transport, "/authors/agent:claude-opus-4.7")
    assert profile["identity_type"] == "agent"
    assert profile["name"] == "Claude Opus 4.7"
    assert profile["agent_handle"] == "agent:claude-opus-4.7"
    assert profile["is_agent"] is True
    assert profile["paper_count"] == 1


def test_agent_profile_models_aggregation() -> None:
    app, transport = _client()
    _seed(app.state.store)
    profile = _get(transport, "/authors/agent:claude-opus-4.7")
    assert len(profile["models"]) == 1
    model = profile["models"][0]
    assert model["name"] == "Claude Opus 4.7"
    assert model["release_pin"] == "claude-opus-4-7-20260520"


def test_co_authors_aggregation() -> None:
    app, transport = _client()
    _seed(app.state.store)
    profile = _get(transport, "/authors/0009-0002-0561-6499")
    assert len(profile["co_authors"]) == 1
    co = profile["co_authors"][0]
    assert co["agent_handle"] == "agent:claude-opus-4.7"
    assert co["is_agent"] is True


def test_papers_subendpoint() -> None:
    app, transport = _client()
    _seed(app.state.store)
    page = _get(transport, "/authors/0009-0002-0561-6499/papers")
    assert len(page["items"]) == 1
    assert page["items"][0]["id"] == "paper-1"


def test_claims_subendpoint_empty() -> None:
    app, transport = _client()
    _seed(app.state.store)
    page = _get(transport, "/authors/0009-0002-0561-6499/claims")
    assert page["items"] == []
    assert page["next_cursor"] is None


def test_unknown_ident_returns_empty_profile() -> None:
    app, transport = _client()
    _seed(app.state.store)
    profile = _get(transport, "/authors/0000-0000-0000-0001")
    assert profile["identity_type"] == "human"
    assert profile["paper_count"] == 0
    assert profile["papers"] == []


def test_unknown_agent_handle_returns_empty_profile() -> None:
    app, transport = _client()
    _seed(app.state.store)
    profile = _get(transport, "/authors/agent:does-not-exist")
    assert profile["identity_type"] == "agent"
    assert profile["paper_count"] == 0
    assert profile["agent_handle"] == "agent:does-not-exist"


def test_list_authors_identity_type_filter() -> None:
    app, transport = _client()
    _seed(app.state.store)
    humans = _get(transport, "/authors", identity_type="human")["items"]
    agents = _get(transport, "/authors", identity_type="agent")["items"]
    assert all(a["identity_type"] == "human" for a in humans)
    assert all(a["identity_type"] == "agent" for a in agents)
    assert any(a["agent_handle"] == "agent:claude-opus-4.7" for a in agents)
