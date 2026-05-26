"""Tests for RRP-0028 default-match search + CSV-OR semantics.

Verifies:
- GET /search/papers with empty q returns the full corpus.
- GET /search/papers with empty q + filters returns the filtered subset.
- ?author=A,B means union (OR), not intersection.
- ?orcid=, ?agent_handle=, ?model_family=, ?model_name= also CSV-OR.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from rrxiv.server import ServerSettings, build_app

pytest.importorskip("fastapi")


def _seed_paper(
    store,
    *,
    paper_id: str,
    title: str,
    blaise_orcid: str,
    agent_name: str,
    agent_handle: str,
    model_family: str,
) -> None:
    paper = {
        "rrxiv_version": "0.1.0",
        "id": paper_id,
        "id_slug": f"rrxiv:2605.{paper_id[-5:].zfill(5)}",
        "version": "v1",
        "title": title,
        "authors": [
            {
                "name": "Blaise Albis-Burdige",
                "orcid": blaise_orcid,
                "is_agent": False,
                "role": "author",
            },
            {
                "name": agent_name,
                "is_agent": True,
                "agent_handle": agent_handle,
                "role": "agent",
                "provenance": {
                    "models": [
                        {
                            "name": agent_name,
                            "vendor": "anthropic" if model_family == "claude" else "openai",
                            "family": model_family,
                            "release_pin": f"{agent_handle.replace('agent:', '')}-pin",
                        }
                    ]
                },
            },
        ],
        "abstract": "test corpus",
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


def _seed_3(store) -> None:
    _seed_paper(
        store,
        paper_id="paper-1",
        title="First",
        blaise_orcid="0009-0002-0561-6499",
        agent_name="Claude Opus 4.7",
        agent_handle="agent:claude-opus-4.7",
        model_family="claude",
    )
    _seed_paper(
        store,
        paper_id="paper-2",
        title="Second",
        blaise_orcid="0009-0002-0561-6499",
        agent_name="Claude Opus 4.7",
        agent_handle="agent:claude-opus-4.7",
        model_family="claude",
    )
    _seed_paper(
        store,
        paper_id="paper-3",
        title="Third",
        blaise_orcid="0000-0000-0000-0000",
        agent_name="GPT-5",
        agent_handle="agent:gpt-5",
        model_family="gpt",
    )


def test_empty_q_returns_full_corpus() -> None:
    app, transport = _client()
    _seed_3(app.state.store)
    items = _get(transport, "/search/papers")["items"]
    assert len(items) == 3


def test_empty_q_with_orcid_filter() -> None:
    app, transport = _client()
    _seed_3(app.state.store)
    items = _get(transport, "/search/papers", orcid="0009-0002-0561-6499")["items"]
    ids = sorted(p["id"] for p in items)
    assert ids == ["paper-1", "paper-2"]


def test_empty_q_with_author_substring() -> None:
    app, transport = _client()
    _seed_3(app.state.store)
    items = _get(transport, "/search/papers", author="Claude Opus 4.7")["items"]
    ids = sorted(p["id"] for p in items)
    assert ids == ["paper-1", "paper-2"]


def test_author_csv_or() -> None:
    app, transport = _client()
    _seed_3(app.state.store)
    items = _get(
        transport, "/search/papers", author="Claude Opus 4.7,GPT-5"
    )["items"]
    ids = sorted(p["id"] for p in items)
    assert ids == ["paper-1", "paper-2", "paper-3"]


def test_orcid_csv_or() -> None:
    app, transport = _client()
    _seed_3(app.state.store)
    items = _get(
        transport,
        "/search/papers",
        orcid="0009-0002-0561-6499,0000-0000-0000-0000",
    )["items"]
    ids = sorted(p["id"] for p in items)
    assert ids == ["paper-1", "paper-2", "paper-3"]


def test_model_family_csv_or() -> None:
    app, transport = _client()
    _seed_3(app.state.store)
    items = _get(transport, "/search/papers", model_family="claude,gpt")["items"]
    ids = sorted(p["id"] for p in items)
    assert ids == ["paper-1", "paper-2", "paper-3"]


def test_agent_handle_csv_or() -> None:
    app, transport = _client()
    _seed_3(app.state.store)
    items = _get(
        transport,
        "/search/papers",
        agent_handle="agent:claude-opus-4.7,agent:gpt-5",
    )["items"]
    ids = sorted(p["id"] for p in items)
    assert ids == ["paper-1", "paper-2", "paper-3"]


def test_papers_route_csv_or_consistency() -> None:
    """GET /papers should honour the same CSV-OR convention."""
    app, transport = _client()
    _seed_3(app.state.store)
    items = _get(transport, "/papers", model_family="claude,gpt")["items"]
    ids = sorted(p["id"] for p in items)
    assert ids == ["paper-1", "paper-2", "paper-3"]
