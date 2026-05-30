"""End-to-end conformance for the annotation post/read round trip.

Sprint 19.P2. The Sprint 17 bug (44 retractions posted, all invisible
because /papers/{id}/claims didn't apply derived status) would have
been caught by one of these. Each test posts an annotation through the
public POST /annotations endpoint and then asserts the read-side
consequence (derived claim status, presence in listings) via the same
endpoints external clients use.

Coverage:

- claim_retraction → claim's replication_status == "retracted" via
  /papers/{paper_id}/claims AND /claims/{claim_id}
- replication (outcome=supports) → claim's replication_status moves
  to "partial" or "replicated" depending on quorum
- comment → annotation listed under /papers/{id}/annotations, no status
  change on the underlying claim
- paper_retraction → annotation persisted + appears in /papers/{id}/annotations
  (does NOT yet cascade to per-claim status — v0.1 server policy; this
  test pins that policy so a future RRP can intentionally relax it)
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from rrxiv.auth import exchange_orcid_code
from rrxiv.server import ServerSettings, build_app


def _client() -> tuple[Any, httpx.Client]:
    from fastapi.testclient import TestClient

    app = build_app(settings=ServerSettings(dev_mode=True))
    tc = TestClient(app)
    transport = tc._transport
    sync = httpx.Client(transport=transport, base_url="http://testserver/api/v0")
    resp = sync.get(
        "/auth/orcid/start",
        params={"redirect_uri": "http://x/cb", "state": "s"},
        follow_redirects=False,
    )
    code = resp.headers["location"].split("code=", 1)[1].split("&")[0]
    bearer = exchange_orcid_code(
        api_base="http://testserver/api/v0",
        code=code,
        state="s",
        expected_state="s",
        transport=transport,
    )
    sync.headers["Authorization"] = f"Bearer {bearer.token}"
    return app, sync


def _seed_paper_with_claim(app: Any, paper_id: str = "p-rt") -> str:
    """Stash a paper + one claim directly in the store. Bypasses the
    submission flow so the test stays focused on annotations.

    Claims are slug-keyed (``claim.paper_id == paper.id_slug``, claim
    ``id`` is ``<id_slug>:<local_label>``) per RRP-0013 / RRP-0029. This
    fixture uses the degenerate ``id == id_slug`` form (so the existing
    ``{paper_id}:c1`` claim-id + ``target_id=<paper_id>`` assertions
    stay valid) — the read filters key off the slug, which here equals
    the id.
    """
    app.state.store.add_paper(
        {
            "rrxiv_version": "0.1.0",
            "id": paper_id,
            "id_slug": paper_id,
            "version": "v1",
            "title": "Round-trip fixture",
            "authors": [{"name": "A. Tester"}],
            "abstract": "abstract",
            "submitted_at": "2026-05-25T00:00:00Z",
            "license": "CC-BY-4.0",
            "source": {"format": "latex", "uri": f"/api/v0/papers/{paper_id}/source"},
        }
    )
    claim_id = f"{paper_id}:c1"
    app.state.store.add_claim(
        {
            "id": claim_id,
            "paper_id": paper_id,
            "kind": "claim",
            "statement": "Test claim.",
            "claim_type": "empirical",
            "evidence_type": "argument",
            "scope": [],
            "replication_status": "untested",
        }
    )
    return claim_id


def _ann_id() -> str:
    return f"ann-{uuid.uuid4().hex[:12]}"


def test_claim_retraction_round_trip() -> None:
    """Post a claim_retraction; the claim's derived status flips to
    "retracted" on both the per-claim and per-paper read paths."""
    app, sync = _client()
    claim_id = _seed_paper_with_claim(app, "p-retr")

    # 1. Status starts as "untested".
    before = sync.get(f"/claims/{claim_id}").json()
    assert before["replication_status"] == "untested", before

    # 2. Post a claim_retraction via the public endpoint.
    resp = sync.post(
        "/annotations",
        json={
            "id": _ann_id(),
            "target_id": claim_id,
            "target_type": "claim",
            "annotation_type": "claim_retraction",
            "content": "Superseded by v2:c1.",
            "structured_payload": {
                "reason": "superseded_by_revision",
                "explanation": "v2 has the corrected statement.",
                "superseded_by_paper": "rrxiv:2605.99999",
                "superseded_by_claim": "rrxiv:2605.99999:claim:c1",
            },
            "created_at": "2026-05-25T18:00:00Z",
            "created_by": {"identity_type": "orcid", "identity": "0000-0001-2345-6789"},
        },
    )
    assert resp.status_code == 201, resp.text

    # 3. Both read paths now reflect the retraction.
    via_claim = sync.get(f"/claims/{claim_id}").json()
    assert via_claim["replication_status"] == "retracted", via_claim

    via_paper = sync.get("/papers/p-retr/claims").json()
    items = via_paper.get("items", [])
    assert items, via_paper
    assert items[0]["replication_status"] == "retracted", items[0]

    sync.close()


def test_replication_round_trip_moves_status_to_partial() -> None:
    """Post a single supporting replication. With quorum >= 1 met
    only for math/formal, an `ml`-topic paper needs >=3; one support
    yields "partial" (RRP-0019 default)."""
    app, sync = _client()
    # No topic on the fixture paper → falls into the "(no tag)" default
    # quorum of 3. One replication therefore should not promote to
    # "replicated" but should leave a visible "partial" trace.
    claim_id = _seed_paper_with_claim(app, "p-rep")

    resp = sync.post(
        "/annotations",
        json={
            "id": _ann_id(),
            "target_id": claim_id,
            "target_type": "claim",
            "annotation_type": "replication",
            "content": "Independent re-run matches within 2pp.",
            "structured_payload": {
                "outcome": "supports",
                "method": "computational",
            },
            "created_at": "2026-05-25T18:00:00Z",
            "created_by": {"identity_type": "orcid", "identity": "0000-0001-2345-6789"},
        },
    )
    assert resp.status_code == 201, resp.text

    derived = sync.get(f"/claims/{claim_id}").json()["replication_status"]
    assert derived == "partial", f"expected partial (1 support, quorum 3), got {derived}"
    sync.close()


def test_comment_round_trip_does_not_change_claim_status() -> None:
    """A `comment` annotation is visible on the paper's annotations
    feed but does not affect the underlying claim's derived status."""
    app, sync = _client()
    claim_id = _seed_paper_with_claim(app, "p-com")

    resp = sync.post(
        "/annotations",
        json={
            "id": _ann_id(),
            "target_id": claim_id,
            "target_type": "claim",
            "annotation_type": "comment",
            "content": "Nice result.",
            "created_at": "2026-05-25T18:00:00Z",
            "created_by": {"identity_type": "orcid", "identity": "0000-0001-2345-6789"},
        },
    )
    assert resp.status_code == 201, resp.text

    derived = sync.get(f"/claims/{claim_id}").json()["replication_status"]
    assert derived == "untested", derived

    # But the comment IS visible on the paper's annotation feed.
    feed = sync.get("/annotations", params={"target_id": claim_id}).json()
    types = {a["annotation_type"] for a in feed.get("items", [])}
    assert "comment" in types, feed
    sync.close()


def test_bulk_endpoint_round_trip() -> None:
    """POST /annotations/bulk accepts an array and returns per-item
    results. Counts as ONE request against the rate limit. Sprint 19.P3."""
    app, sync = _client()
    claim_id = _seed_paper_with_claim(app, "p-bulk")

    bulk_body = {
        "annotations": [
            {
                "id": _ann_id(),
                "target_id": claim_id,
                "target_type": "claim",
                "annotation_type": "comment",
                "content": "First comment in batch.",
                "created_at": "2026-05-25T18:00:00Z",
                "created_by": {"identity_type": "orcid", "identity": "0000-0001-2345-6789"},
            },
            {
                "id": _ann_id(),
                "target_id": claim_id,
                "target_type": "claim",
                "annotation_type": "claim_retraction",
                "content": "Retracting via bulk.",
                "structured_payload": {"reason": "data_error"},
                "created_at": "2026-05-25T18:00:01Z",
                "created_by": {"identity_type": "orcid", "identity": "0000-0001-2345-6789"},
            },
            # One deliberately bad one — bulk should not abort, just
            # report per-index status.
            {
                "id": _ann_id(),
                "target_id": "nonexistent:c9",
                "target_type": "claim",
                "annotation_type": "comment",
                "content": "Targets a claim that doesn't exist.",
                "created_at": "2026-05-25T18:00:02Z",
                "created_by": {"identity_type": "orcid", "identity": "0000-0001-2345-6789"},
            },
        ]
    }
    resp = sync.post("/annotations/bulk", json=bulk_body)
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert len(results) == 3
    assert results[0]["status"] == 201
    assert results[1]["status"] == 201
    assert results[2]["status"] == 404  # claim missing

    # The retraction took effect (read-side derivation reflects it).
    derived = sync.get(f"/claims/{claim_id}").json()["replication_status"]
    assert derived == "retracted", derived
    sync.close()


def test_bulk_endpoint_rejects_oversize() -> None:
    """Bulk caps at 100 items per request; 101 → 400."""
    app, sync = _client()
    _seed_paper_with_claim(app, "p-big")
    ann = {
        "id": "x",
        "target_id": "p-big:c1",
        "target_type": "claim",
        "annotation_type": "comment",
        "content": "x",
        "created_at": "2026-05-25T18:00:00Z",
        "created_by": {"identity_type": "orcid", "identity": "0000-0001-2345-6789"},
    }
    resp = sync.post("/annotations/bulk", json={"annotations": [ann] * 101})
    assert resp.status_code == 400, resp.text
    sync.close()


def test_paper_retraction_does_not_cascade_to_claim_status() -> None:
    """A paper_retraction is persisted + listed but doesn't currently
    auto-retract every claim. This pins the v0.1 policy — a future RRP
    may relax it; the test will need updating then."""
    app, sync = _client()
    claim_id = _seed_paper_with_claim(app, "p-prtr")

    resp = sync.post(
        "/annotations",
        json={
            "id": _ann_id(),
            "target_id": "p-prtr",
            "target_type": "paper",
            "annotation_type": "paper_retraction",
            "content": "Whole-paper retraction.",
            "structured_payload": {
                "reason": "data_error",
                "explanation": "Authors withdraw following audit.",
            },
            "created_at": "2026-05-25T18:00:00Z",
            "created_by": {"identity_type": "orcid", "identity": "0000-0001-2345-6789"},
        },
    )
    assert resp.status_code == 201, resp.text

    # Pinned: paper-level retraction does NOT cascade to claims in v0.1.
    derived = sync.get(f"/claims/{claim_id}").json()["replication_status"]
    assert derived == "untested", derived

    # But the annotation IS persisted + listed under the paper.
    feed = sync.get("/annotations", params={"target_id": "p-prtr"}).json()
    types = {a["annotation_type"] for a in feed.get("items", [])}
    assert "paper_retraction" in types, feed
    sync.close()
