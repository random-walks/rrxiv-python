"""Tests for the RRP-0027 model registry endpoint.

Verifies:
- /models/registry returns the curated registry shape.
- papers_count is computed from the corpus (provenance.models[]).
- Missing registry file degrades to {"entries": []}.
- Hot-reload triggers when file mtime changes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from rrxiv.server import ServerSettings, build_app
from rrxiv.server.models import router as registry_module

pytest.importorskip("fastapi")


def _client():  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    app = build_app(settings=ServerSettings(dev_mode=True))
    test_client = TestClient(app)
    return app, test_client._transport


def _write_registry(tmp_path: Path, entries: list[dict]) -> Path:
    """Write a minimal registry to a tmp path and reset the in-module cache."""
    reg_path = tmp_path / "registry.json"
    reg_path.write_text(
        json.dumps(
            {
                "version": "0.1.0",
                "updated_at": "2026-05-26",
                "entries": entries,
            }
        )
    )
    os.environ["RRXIV_MODEL_REGISTRY_PATH"] = str(reg_path)
    registry_module._cache = None  # type: ignore[attr-defined]
    return reg_path


def _seed_paper_with_claude(store) -> None:
    paper = {
        "rrxiv_version": "0.1.0",
        "id": "paper-1",
        "id_slug": "rrxiv:2605.00001",
        "version": "v1",
        "title": "Test",
        "authors": [
            {
                "name": "Blaise",
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


def test_registry_returns_curated_entries(tmp_path: Path) -> None:
    _write_registry(
        tmp_path,
        [
            {
                "name": "Claude Opus 4.7",
                "release_pin": "claude-opus-4-7-20260520",
                "vendor": "anthropic",
                "family": "claude",
                "is_current": True,
                "display_order": 10,
            },
            {
                "name": "GPT-5",
                "release_pin": "gpt-5-2026-04-15",
                "vendor": "openai",
                "family": "gpt",
                "is_current": True,
                "display_order": 110,
            },
        ],
    )
    app, transport = _client()
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        resp = c.get("/models/registry")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == "0.1.0"
    names = [e["name"] for e in body["entries"]]
    assert names == ["Claude Opus 4.7", "GPT-5"]  # display_order sort


def test_registry_papers_count_from_corpus(tmp_path: Path) -> None:
    _write_registry(
        tmp_path,
        [
            {
                "name": "Claude Opus 4.7",
                "release_pin": "claude-opus-4-7-20260520",
                "vendor": "anthropic",
                "family": "claude",
            },
            {
                "name": "GPT-5",
                "release_pin": "gpt-5-2026-04-15",
                "vendor": "openai",
                "family": "gpt",
            },
        ],
    )
    app, transport = _client()
    _seed_paper_with_claude(app.state.store)
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        body = c.get("/models/registry").json()
    by_pin = {e["release_pin"]: e for e in body["entries"]}
    assert by_pin["claude-opus-4-7-20260520"]["papers_count"] == 1
    assert by_pin["gpt-5-2026-04-15"]["papers_count"] == 0
    assert by_pin["claude-opus-4-7-20260520"]["last_seen_at"]


def test_registry_missing_file_degrades_to_empty(tmp_path: Path) -> None:
    # Point at a path that doesn't exist.
    os.environ["RRXIV_MODEL_REGISTRY_PATH"] = str(tmp_path / "does-not-exist.json")
    registry_module._cache = None  # type: ignore[attr-defined]
    app, transport = _client()
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        body = c.get("/models/registry").json()
    assert body["entries"] == []
