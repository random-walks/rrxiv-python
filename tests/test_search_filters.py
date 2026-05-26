"""Tests for the RRP-0026 targeted search filters on GET /search/papers.

Confirms the four new query params (?orcid, ?agent_handle, ?model_family,
?model_name) correctly filter against the structured author shape.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from rrxiv.server import ServerSettings, build_app

pytest.importorskip("fastapi")


def _seed_one_paper(store, *, paper_id: str, claude_name: str, claude_handle: str,
                    model_name: str, model_family: str, model_release_pin: str,
                    blaise_orcid: str = "0009-0002-0561-6499") -> None:
    paper = {
        "rrxiv_version": "0.1.0",
        "id": paper_id,
        "id_slug": f"rrxiv:2605.{paper_id[-5:].zfill(5)}",
        "version": "v1",
        "title": f"Test paper {paper_id}",
        "authors": [
            {
                "name": "Blaise Albis-Burdige",
                "orcid": blaise_orcid,
                "is_agent": False,
                "role": "author",
            },
            {
                "name": claude_name,
                "is_agent": True,
                "agent_handle": claude_handle,
                "role": "agent",
                "provenance": {
                    "models": [
                        {
                            "name": model_name,
                            "vendor": "anthropic" if "claude" in model_family else model_family,
                            "family": model_family,
                            "release_pin": model_release_pin,
                        }
                    ],
                    "inference_environment": "Claude Code CLI",
                    "operator_orcid": blaise_orcid,
                },
            },
        ],
        "abstract": "test",
        "submitted_at": "2026-05-26T18:00:00Z",
        "license": "CC-BY-4.0",
        "source": {"format": "latex", "uri": "/x.tar.gz"},
    }
    store.add_paper(paper)
    # Add a minimal CIR mirror so to_list_item() projects correctly.
    cir = dict(paper)
    cir["claims"] = []
    store.add_cir(cir)


def _client():  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    app = build_app(settings=ServerSettings(dev_mode=True))
    test_client = TestClient(app)
    return app, test_client._transport


def _search(transport: httpx.BaseTransport, **params: Any) -> list[dict[str, Any]]:
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        resp = c.get("/search/papers", params={"q": "test", **params})
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def test_orcid_filter_exact_match() -> None:
    app, transport = _client()
    _seed_one_paper(
        app.state.store,
        paper_id="paper-1",
        claude_name="Claude Opus 4.7",
        claude_handle="agent:claude-opus-4.7",
        model_name="Claude Opus 4.7",
        model_family="claude",
        model_release_pin="claude-opus-4-7-20260520",
        blaise_orcid="0009-0002-0561-6499",
    )
    _seed_one_paper(
        app.state.store,
        paper_id="paper-2",
        claude_name="Claude Opus 4.7",
        claude_handle="agent:claude-opus-4.7",
        model_name="Claude Opus 4.7",
        model_family="claude",
        model_release_pin="claude-opus-4-7-20260520",
        blaise_orcid="0000-0000-0000-0000",  # different human
    )
    matches = _search(transport, orcid="0009-0002-0561-6499")
    assert len(matches) == 1
    assert matches[0]["id"] == "paper-1"


def test_agent_handle_filter_exact_match() -> None:
    app, transport = _client()
    _seed_one_paper(
        app.state.store,
        paper_id="paper-claude",
        claude_name="Claude Opus 4.7",
        claude_handle="agent:claude-opus-4.7",
        model_name="Claude Opus 4.7",
        model_family="claude",
        model_release_pin="claude-opus-4-7-20260520",
    )
    _seed_one_paper(
        app.state.store,
        paper_id="paper-gpt",
        claude_name="GPT-5",
        claude_handle="agent:gpt-5",
        model_name="GPT-5",
        model_family="gpt",
        model_release_pin="gpt-5-2026-04-15",
    )
    matches = _search(transport, agent_handle="agent:claude-opus-4.7")
    ids = sorted(p["id"] for p in matches)
    assert ids == ["paper-claude"]


def test_model_family_filter() -> None:
    app, transport = _client()
    _seed_one_paper(
        app.state.store,
        paper_id="paper-claude",
        claude_name="Claude Opus 4.7",
        claude_handle="agent:claude-opus-4.7",
        model_name="Claude Opus 4.7",
        model_family="claude",
        model_release_pin="claude-opus-4-7-20260520",
    )
    _seed_one_paper(
        app.state.store,
        paper_id="paper-gpt",
        claude_name="GPT-5",
        claude_handle="agent:gpt-5",
        model_name="GPT-5",
        model_family="gpt",
        model_release_pin="gpt-5-2026-04-15",
    )
    matches = _search(transport, model_family="claude")
    ids = sorted(p["id"] for p in matches)
    assert ids == ["paper-claude"]
    # Case-insensitive match too
    matches_upper = _search(transport, model_family="CLAUDE")
    assert sorted(p["id"] for p in matches_upper) == ["paper-claude"]


def test_model_name_substring_filter() -> None:
    app, transport = _client()
    _seed_one_paper(
        app.state.store,
        paper_id="paper-opus",
        claude_name="Claude Opus 4.7",
        claude_handle="agent:claude-opus-4.7",
        model_name="Claude Opus 4.7",
        model_family="claude",
        model_release_pin="claude-opus-4-7-20260520",
    )
    _seed_one_paper(
        app.state.store,
        paper_id="paper-sonnet",
        claude_name="Claude Sonnet 4.5",
        claude_handle="agent:claude-sonnet-4.5",
        model_name="Claude Sonnet 4.5",
        model_family="claude",
        model_release_pin="claude-sonnet-4-5-20260301",
    )
    # Substring filter — case insensitive
    matches = _search(transport, model_name="opus")
    assert sorted(p["id"] for p in matches) == ["paper-opus"]
    matches_general = _search(transport, model_name="Claude")
    assert sorted(p["id"] for p in matches_general) == ["paper-opus", "paper-sonnet"]
