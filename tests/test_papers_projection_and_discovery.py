"""Tests for the paper list-item projection (RRP-0012),
the discovery endpoints (/scopes, /topics, /claims/top), and the
id_slug pattern (RRP-0013).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from rrxiv.server import ServerSettings, build_app
from rrxiv.server.papers.projection import compute_stats, to_list_item
from rrxiv.server.papers.scopes import SCOPES, filter_by_scope
from rrxiv.server.papers.slug import is_slug, mint_slug, slug_yymm
from rrxiv.server.store import MemoryStore

pytest.importorskip("fastapi")


# ---------- Slug helpers --------------------------------------------------


def test_slug_pattern_recognition() -> None:
    assert is_slug("rrxiv:2402.00128")
    assert is_slug("rrxiv:2605.00001")
    assert not is_slug("01923f8e-5b2a-7c4d-9e1f-3a2b1c0d4e5f")
    assert not is_slug("rrxiv:2402.128")  # too few digits in counter
    assert not is_slug("arxiv:2402.00128")  # wrong prefix


def test_slug_mint_increments_within_month() -> None:
    store = MemoryStore()
    yymm = slug_yymm()
    first = mint_slug(store)
    assert first == f"rrxiv:{yymm}.00001"
    # Persist a paper carrying that slug; next mint should advance.
    store.add_paper(
        {
            "id": "uuid-1",
            "id_slug": first,
            "submitted_at": "2026-05-04T12:00:00Z",
        }
    )
    second = mint_slug(store)
    assert second == f"rrxiv:{yymm}.00002"


def test_slug_mint_ignores_other_months() -> None:
    store = MemoryStore()
    # Sprinkle a "different month" slug.
    store.add_paper({"id": "uuid-1", "id_slug": "rrxiv:2401.05000"})
    yymm = slug_yymm()
    if yymm == "2401":
        # If today happens to be Jan 2024-relative, skip — irrelevant edge.
        pytest.skip("test month coincidence")
    minted = mint_slug(store)
    assert minted == f"rrxiv:{yymm}.00001"


# ---------- Projection (compute_stats / to_list_item) --------------------


def _paper(paper_id: str = "p1") -> dict[str, Any]:
    return {
        "rrxiv_version": "0.1.0",
        "id": paper_id,
        "version": "v1",
        "title": "Title",
        "authors": [{"name": "A. Author"}],
        "abstract": "abs",
        "submitted_at": "2026-05-04T00:00:00Z",
        "license": "CC-BY-4.0",
        "source": {"format": "latex", "uri": "https://x.org/p.tar.gz"},
        "topics": ["infrastructure"],
    }


def _claim(claim_id: str, paper_id: str, status: str = "untested") -> dict[str, Any]:
    return {
        "id": claim_id,
        "paper_id": paper_id,
        "statement": "Stub claim.",
        "claim_type": "theoretical",
        "evidence_type": "argument",
        "replication_status": status,
    }


def test_compute_stats_preprint_no_claims() -> None:
    store = MemoryStore()
    stats = compute_stats("p1", store)
    assert stats["claims"] == 0
    assert stats["status"] == "preprint"
    assert "computed_at" in stats


def test_compute_stats_preprint_all_untested_no_annotations() -> None:
    store = MemoryStore()
    store.add_paper(_paper("p1"))
    store.add_claim(_claim("p1:c1", "p1"))
    store.add_claim(_claim("p1:c2", "p1"))
    stats = compute_stats("p1", store)
    assert stats["claims"] == 2
    assert stats["untested"] == 2
    assert stats["status"] == "preprint"


def test_compute_stats_replicated() -> None:
    store = MemoryStore()
    store.add_paper(_paper("p1"))
    store.add_claim(_claim("p1:c1", "p1", status="replicated"))
    store.add_claim(_claim("p1:c2", "p1", status="replicated"))
    stats = compute_stats("p1", store)
    assert stats["replicated"] == 2
    assert stats["status"] == "replicated"


def test_compute_stats_contested_mixed() -> None:
    store = MemoryStore()
    store.add_paper(_paper("p1"))
    store.add_claim(_claim("p1:c1", "p1", status="replicated"))
    store.add_claim(_claim("p1:c2", "p1", status="contradicted"))
    stats = compute_stats("p1", store)
    assert stats["replicated"] == 1
    assert stats["contradicted"] == 1
    assert stats["status"] == "contested"


def test_compute_stats_partial_not_contested() -> None:
    """Regression: a paper whose only annotated claims have ``partial``
    status (1 replication, no contradiction) must NOT roll up to
    ``contested``. Earlier the projection counted partial claims into
    the contested bucket, so Euclid (0 contradictions, 3 partials)
    surfaced as ``status='contested'`` on the home page.

    With the new mapping a paper with partials + no contradicts rolls
    up to ``partial``; ``contested`` is reserved for genuine mixed
    evidence.
    """
    store = MemoryStore()
    store.add_paper(_paper("p1"))
    store.add_claim(_claim("p1:c1", "p1", status="partial"))
    store.add_claim(_claim("p1:c2", "p1", status="untested"))

    stats = compute_stats("p1", store)
    assert stats["partial"] == 1
    assert stats["contested"] == 0, (
        "partial claims must not be counted under contested"
    )
    assert stats["contradicted"] == 0
    assert stats["status"] == "partial", (
        "paper-level status must be 'partial' not 'contested' when "
        "only partials + untesteds exist"
    )


def test_compute_stats_replicated_with_partials_rolls_up_to_replicated() -> None:
    """A paper where most claims are replicated and a few are partial
    rolls up to 'replicated' (the per-claim partials don't downgrade
    the paper). Euclid's reality: 462 replicated + 3 partial + 0
    contradicted should be 'replicated', not 'contested'.
    """
    store = MemoryStore()
    store.add_paper(_paper("p1"))
    store.add_claim(_claim("p1:c1", "p1", status="replicated"))
    store.add_claim(_claim("p1:c2", "p1", status="replicated"))
    store.add_claim(_claim("p1:c3", "p1", status="partial"))

    stats = compute_stats("p1", store)
    assert stats["replicated"] == 2
    assert stats["partial"] == 1
    assert stats["status"] == "replicated"


def test_compute_stats_retracted() -> None:
    store = MemoryStore()
    store.add_paper(_paper("p1"))
    store.add_annotation(
        {
            "id": "ann-1",
            "target_id": "p1",
            "target_type": "paper",
            "annotation_type": "erratum",
            "content": "Retracted by author.",
            "structured_payload": {"retracted": True},
            "created_at": "2026-05-04T12:00:00Z",
            "created_by": {"identity_type": "orcid", "identity": "0000-0001-0000-0001"},
        }
    )
    stats = compute_stats("p1", store)
    assert stats["status"] == "retracted"


def test_to_list_item_wraps_paper_with_stats() -> None:
    store = MemoryStore()
    paper = _paper("p1")
    store.add_paper(paper)
    item = to_list_item(paper, store)
    # Paper fields preserved...
    assert item["id"] == "p1"
    assert item["title"] == "Title"
    # ...and stats attached.
    assert "stats" in item
    assert item["stats"]["claims"] == 0


# ---------- Scope predicates --------------------------------------------


def test_scopes_defined() -> None:
    ids = {s["id"] for s in SCOPES}
    assert {"all", "active", "agent", "human", "contested", "fresh"} <= ids


def test_filter_by_scope_unknown_returns_all() -> None:
    items: list[dict[str, Any]] = [{"id": "p1", "stats": {"status": "replicated"}}]
    assert filter_by_scope(items, "doesnotexist") == items


def test_filter_by_scope_contested() -> None:
    items: list[dict[str, Any]] = [
        {"id": "p1", "stats": {"status": "contested", "contradicted": 1}},
        {"id": "p2", "stats": {"status": "replicated", "contradicted": 0}},
    ]
    result = filter_by_scope(items, "contested")
    assert [i["id"] for i in result] == ["p1"]


# ---------- Endpoint integration tests ----------------------------------


def _build_app_and_transport():  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    settings = ServerSettings(dev_mode=True)
    app = build_app(settings=settings)
    test_client = TestClient(app)
    return app, test_client._transport


def test_list_papers_returns_list_item_shape() -> None:
    app, transport = _build_app_and_transport()
    app.state.store.add_paper(_paper("p1"))
    app.state.store.add_claim(_claim("p1:c1", "p1", status="replicated"))
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        resp = c.get("/papers")
    assert resp.status_code == 200
    body = resp.json()
    items = body["items"]
    assert len(items) == 1
    assert items[0]["id"] == "p1"
    assert "stats" in items[0]
    assert items[0]["stats"]["replicated"] == 1
    assert items[0]["stats"]["status"] == "replicated"


def test_list_papers_scope_filter() -> None:
    app, transport = _build_app_and_transport()
    p_replicated = _paper("p1")
    p_preprint = _paper("p2")
    app.state.store.add_paper(p_replicated)
    app.state.store.add_paper(p_preprint)
    app.state.store.add_claim(_claim("p1:c1", "p1", status="replicated"))
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        all_resp = c.get("/papers")
        contested_resp = c.get("/papers", params={"scope": "contested"})
    assert {i["id"] for i in all_resp.json()["items"]} == {"p1", "p2"}
    # Neither paper is "contested" — both should be filtered out.
    assert contested_resp.json()["items"] == []


def test_list_papers_hides_superseded_versions_by_default() -> None:
    """Per RRP-0017, the listing returns one row per lineage.

    When v2 of a paper exists with ``previous_version=v1.id``, the
    listing should show v2 and hide v1 (the head-of-lineage rule).
    Audit views can opt in via ``?include_superseded=1``.
    """
    app, transport = _build_app_and_transport()

    v1 = _paper("paper-v1")
    v2 = _paper("paper-v2")
    v2["previous_version"] = "paper-v1"
    # Unrelated paper — should always show.
    other = _paper("other")

    app.state.store.add_paper(v1)
    app.state.store.add_paper(v2)
    app.state.store.add_paper(other)

    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        default_resp = c.get("/papers")
        archival_resp = c.get("/papers", params={"include_superseded": "true"})

    default_ids = {i["id"] for i in default_resp.json()["items"]}
    archival_ids = {i["id"] for i in archival_resp.json()["items"]}

    # Default: v1 hidden because v2 supersedes it. v2 + other shown.
    assert default_ids == {"paper-v2", "other"}, default_ids
    # Archival: all three versions visible.
    assert archival_ids == {"paper-v1", "paper-v2", "other"}, archival_ids


def test_paper_detail_include_stats() -> None:
    app, transport = _build_app_and_transport()
    app.state.store.add_paper(_paper("p1"))
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        plain = c.get("/papers/p1")
        with_stats = c.get("/papers/p1", params={"include": "stats"})
    assert plain.status_code == 200
    assert "stats" not in plain.json()
    assert with_stats.status_code == 200
    assert "stats" in with_stats.json()


def test_paper_resolve_by_slug() -> None:
    app, transport = _build_app_and_transport()
    paper = _paper("uuid-p1")
    paper["id_slug"] = "rrxiv:2605.00099"
    app.state.store.add_paper(paper)
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        by_slug = c.get("/papers/rrxiv:2605.00099")
        by_id = c.get("/papers/uuid-p1")
    assert by_slug.status_code == 200
    assert by_id.status_code == 200
    assert by_slug.json()["id"] == "uuid-p1"


def test_paper_resolve_by_slug_returns_head_of_lineage() -> None:
    """Per RRP-0013, slugs are inherited across revisions — multiple
    paper rows share a slug. The slug resolver must return the
    HEAD (latest version), not whatever's first in insertion order.

    Regression: earlier the resolver returned v1 even when v4
    existed in the lineage, so ``/papers/rrxiv:2605.00001`` on the
    web rendered stale v1 data.
    """
    app, transport = _build_app_and_transport()

    v1 = _paper("paper-v1")
    v1["id_slug"] = "rrxiv:2605.00099"
    v1["version"] = "v1"
    v1["submitted_at"] = "2026-05-01T10:00:00Z"

    v2 = _paper("paper-v2")
    v2["id_slug"] = "rrxiv:2605.00099"
    v2["version"] = "v2"
    v2["previous_version"] = "paper-v1"
    v2["submitted_at"] = "2026-05-15T10:00:00Z"

    v3 = _paper("paper-v3")
    v3["id_slug"] = "rrxiv:2605.00099"
    v3["version"] = "v3"
    v3["previous_version"] = "paper-v2"
    v3["submitted_at"] = "2026-05-22T10:00:00Z"

    app.state.store.add_paper(v1)
    app.state.store.add_paper(v2)
    app.state.store.add_paper(v3)

    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        resp = c.get("/papers/rrxiv:2605.00099")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "paper-v3", (
        f"slug resolution must return head-of-lineage v3, got {body['id']!r}"
    )
    assert body["version"] == "v3"


def test_papers_claims_endpoint() -> None:
    app, transport = _build_app_and_transport()
    app.state.store.add_paper(_paper("p1"))
    app.state.store.add_claim(_claim("p1:c1", "p1"))
    app.state.store.add_claim(_claim("p1:c2", "p1"))
    app.state.store.add_claim(_claim("p2:c1", "p2"))  # different paper
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        resp = c.get("/papers/p1/claims")
    assert resp.status_code == 200
    ids = {c["id"] for c in resp.json()["items"]}
    assert ids == {"p1:c1", "p1:c2"}


def test_papers_claims_endpoint_reflects_derived_retraction() -> None:
    """Regression for the Sprint 14 v1→v2 dogfood: a claim_retraction
    annotation must surface through /papers/{id}/claims so readers see
    the derived status, not the persisted one. Before this fix, the
    route returned raw claims and 44 retraction annotations posted
    against the v1 records didn't visibly change anything."""
    app, transport = _build_app_and_transport()
    app.state.store.add_paper(_paper("p-retr"))
    app.state.store.add_claim(_claim("p-retr:c1", "p-retr"))
    # Post-submission retraction annotation by the same identity that
    # would be the author in the real flow.
    app.state.store.add_annotation(
        {
            "id": "ann-retract-c1",
            "target_id": "p-retr:c1",
            "target_type": "claim",
            "annotation_type": "claim_retraction",
            "content": "Superseded by p-retr-v2:c1.",
            "structured_payload": {"reason": "superseded_by_revision"},
            "created_at": "2026-05-25T18:00:00Z",
            "created_by": {"identity_type": "agent", "identity": "@deployer"},
        }
    )
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        resp = c.get("/papers/p-retr/claims")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["replication_status"] == "retracted", items[0]


def test_papers_related_topic_overlap() -> None:
    app, transport = _build_app_and_transport()
    p1 = _paper("p1")
    p1["topics"] = ["physics", "infrastructure"]
    p2 = _paper("p2")
    p2["topics"] = ["physics", "biology"]
    p3 = _paper("p3")
    p3["topics"] = ["unrelated"]
    app.state.store.add_paper(p1)
    app.state.store.add_paper(p2)
    app.state.store.add_paper(p3)
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        resp = c.get("/papers/p1/related", params={"limit": 5})
    items = resp.json()["items"]
    # p2 shares a topic with p1; p3 doesn't.
    assert [i["id"] for i in items] == ["p2"]
    assert "stats" in items[0]


def test_discovery_scopes_endpoint() -> None:
    _, transport = _build_app_and_transport()
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        resp = c.get("/scopes")
    assert resp.status_code == 200
    ids = {s["id"] for s in resp.json()["items"]}
    assert {"all", "active", "agent", "human", "contested", "fresh"} <= ids


def test_discovery_topics_endpoint() -> None:
    app, transport = _build_app_and_transport()
    p1 = _paper("p1")
    p1["topics"] = ["physics", "infrastructure"]
    p2 = _paper("p2")
    p2["topics"] = ["physics", "biology"]
    app.state.store.add_paper(p1)
    app.state.store.add_paper(p2)
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        resp = c.get("/topics")
    assert resp.status_code == 200
    # Sorted unique union.
    assert resp.json()["items"] == ["biology", "infrastructure", "physics"]


def test_discovery_topics_with_counts() -> None:
    app, transport = _build_app_and_transport()
    p1 = _paper("p1")
    p1["topics"] = ["physics", "infrastructure"]
    p2 = _paper("p2")
    p2["topics"] = ["physics", "biology"]
    p3 = _paper("p3")
    p3["topics"] = ["physics"]
    app.state.store.add_paper(p1)
    app.state.store.add_paper(p2)
    app.state.store.add_paper(p3)
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        resp = c.get("/topics", params={"with_counts": 1})
    assert resp.status_code == 200
    items = resp.json()["items"]
    # Sorted by count desc, then alpha.
    assert items[0] == {"topic": "physics", "count": 3}
    assert {"topic": "biology", "count": 1} in items
    assert {"topic": "infrastructure", "count": 1} in items


def test_discovery_stats_by_status() -> None:
    app, transport = _build_app_and_transport()
    p1 = _paper("p1")  # preprint — no claims
    p2 = _paper("p2")  # untested — claims, no annotations
    p3 = _paper("p3")  # replicated — claim replicated
    app.state.store.add_paper(p1)
    app.state.store.add_paper(p2)
    app.state.store.add_paper(p3)
    app.state.store.add_claim(_claim("p2:c1", "p2", "untested"))
    app.state.store.add_claim(_claim("p3:c1", "p3", "replicated"))
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        resp = c.get("/stats")
    assert resp.status_code == 200
    by_status = resp.json()["by_status"]
    # p1 (no claims) + p2 (untested with 1 claim, no annotations -> preprint)
    assert by_status["preprint"] == 2
    assert by_status["replicated"] == 1
    assert by_status["contested"] == 0
    assert by_status["retracted"] == 0


def test_claims_top_endpoint() -> None:
    app, transport = _build_app_and_transport()
    app.state.store.add_paper(_paper("p1"))
    # c1 is depended on by c2 + c3; should rank above c4 (no incoming edges).
    app.state.store.add_claim(_claim("p1:c1", "p1"))
    c2 = _claim("p1:c2", "p1")
    c2["depends_on"] = ["p1:c1"]
    c3 = _claim("p1:c3", "p1")
    c3["depends_on"] = ["p1:c1"]
    app.state.store.add_claim(c2)
    app.state.store.add_claim(c3)
    app.state.store.add_claim(_claim("p1:c4", "p1"))
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        resp = c.get("/claims/top", params={"limit": 5})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 4
    for item in items:
        # ``queries`` retained as backward-compat alias of dependents_count.
        assert {"id", "statement", "queries", "dependents_count"} <= item.keys()
    # c1 has 2 dependents → must rank first.
    assert items[0]["id"] == "p1:c1"
    assert items[0]["dependents_count"] == 2


def test_search_papers_returns_list_item_shape() -> None:
    app, transport = _build_app_and_transport()
    paper = _paper("p1")
    paper["title"] = "Queryable protocols for preprints"
    app.state.store.add_paper(paper)
    with httpx.Client(transport=transport, base_url="http://test/api/v0") as c:
        resp = c.get("/search/papers", params={"q": "queryable"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert "stats" in items[0]


def test_cors_origins_env_allowlist() -> None:
    """The settings parser should split a comma-separated env var into a tuple."""
    settings = ServerSettings.from_env(
        environ={
            "RRXIV_API_BASE": "http://localhost:8000/api/v0",
            "RRXIV_STORE_URL": "memory://",
            "RRXIV_DEV_MODE": "1",
            "RRXIV_CORS_ORIGINS": "https://rrxiv.org,https://www.rrxiv.org",
        }
    )
    assert settings.cors_origins == (
        "https://rrxiv.org",
        "https://www.rrxiv.org",
    )
