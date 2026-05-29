"""Neighborhood endpoint embeds neighbour claim previews (Sprint 27).

Regression guard for the N+1 fan-out that timed out the web render on
high-degree claims (e.g. Euclid ``prop:I.34``, ~20 neighbours): the
server now returns a ``claims`` map keyed by neighbour id so the client
needs a single request instead of one ``GET /claims/{id}`` per edge.
"""

from __future__ import annotations

from typing import Any

import pytest

from rrxiv.server import ServerSettings, build_app

pytest.importorskip("fastapi")


def _paper(paper_id: str = "p1") -> dict[str, Any]:
    return {
        "rrxiv_version": "0.1.0",
        "id": paper_id,
        "version": "v1",
        "title": "T",
        "authors": [{"name": "A. Author"}],
        "abstract": "x",
        "submitted_at": "2026-05-04T00:00:00Z",
        "license": "CC-BY-4.0",
        "source": {"format": "latex", "uri": "https://x.org/p.tar.gz"},
    }


def _claim(
    claim_id: str,
    paper_id: str,
    statement: str,
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": claim_id,
        "paper_id": paper_id,
        "statement": statement,
        "claim_type": "theoretical",
        "evidence_type": "argument",
        "replication_status": "untested",
        "depends_on": depends_on or [],
    }


def _client():  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    app = build_app(settings=ServerSettings(dev_mode=True))
    return app, TestClient(app)


def test_neighborhood_embeds_outgoing_neighbor_previews() -> None:
    app, client = _client()
    store = app.state.store
    store.add_paper(_paper("p1"))
    store.add_claim(
        _claim("p1:prop:A", "p1", "Statement A.", ["p1:prop:B", "p1:prop:C"])
    )
    store.add_claim(_claim("p1:prop:B", "p1", "Statement B."))
    store.add_claim(_claim("p1:prop:C", "p1", "Statement C."))

    resp = client.get("/api/v0/claims/p1:prop:A/neighborhood")
    assert resp.status_code == 200
    body = resp.json()

    # Edges still present and unchanged.
    assert {e["target"] for e in body["depends_on"]} == {"p1:prop:B", "p1:prop:C"}

    # The embedded claims map lets the client skip the per-neighbour
    # fetch entirely: it carries each neighbour's statement + type.
    claims = body["claims"]
    assert set(claims) == {"p1:prop:B", "p1:prop:C"}
    assert claims["p1:prop:B"]["statement"] == "Statement B."
    assert claims["p1:prop:C"]["statement"] == "Statement C."
    assert claims["p1:prop:B"]["claim_type"] == "theoretical"
    # Origin is not echoed into its own neighbour map.
    assert "p1:prop:A" not in claims


def test_neighborhood_embeds_incoming_neighbor_previews() -> None:
    app, client = _client()
    store = app.state.store
    store.add_paper(_paper("p1"))
    store.add_claim(_claim("p1:prop:A", "p1", "Statement A."))
    # B depends on A → A's neighborhood lists B as a dependent and embeds
    # B's preview (the inbound end is resolved too, not just outbound).
    store.add_claim(_claim("p1:prop:B", "p1", "Statement B.", ["p1:prop:A"]))

    body = client.get("/api/v0/claims/p1:prop:A/neighborhood").json()
    assert {e["source"] for e in body["dependents"]} == {"p1:prop:B"}
    assert body["claims"]["p1:prop:B"]["statement"] == "Statement B."


def test_neighborhood_unknown_claim_returns_empty_claims_map() -> None:
    _app, client = _client()
    body = client.get("/api/v0/claims/p1:prop:missing/neighborhood").json()
    assert body["claims"] == {}
    assert body["depends_on"] == []
    assert body["dependents"] == []
