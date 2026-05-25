"""SRV-sprint Phase 2 server tests.

Covers the new endpoints + behaviours wired by RRPs 0016-0020:

- Revision diff computation + endpoint (RRP-0017)
- Errata listing endpoint (RRP-0017 companion)
- Annotation in_reply_to validation + replies endpoint (RRP-0018)
- claim_retraction → replication_status=retracted derivation (RRP-0020)
- replication annotations driving derived status (RRP-0019)
- dry_run submission mode (RRP-0016)
- bundle_hash_mismatch detection (RRP-0016)
- Synthesised revision_summary annotation on revision submit
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest

from rrxiv.auth import exchange_orcid_code
from rrxiv.server import ServerSettings, build_app
from rrxiv.server.claims.replication import (
    derive_replication_status,
    quorum_for_claim,
)
from rrxiv.server.papers.diff import (
    claim_local_id,
    compute_revision_diff,
    papers_in_same_lineage,
)
from rrxiv.server.store import MemoryStore

pytest.importorskip("fastapi")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _paper(
    paper_id: str,
    *,
    version: str = "v1",
    previous_version: str | None = None,
    abstract: str = "abstract",
    topics: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "rrxiv_version": "0.1.0",
        "id": paper_id,
        "version": version,
        "previous_version": previous_version,
        "title": f"Paper {paper_id}",
        "authors": [{"name": "A. Author", "orcid": "0000-0001-2345-6789"}],
        "abstract": abstract,
        "submitted_at": "2026-05-04T00:00:00Z",
        "license": "CC-BY-4.0",
        "source": {"format": "latex", "uri": f"https://x.org/{paper_id}.tar.gz"},
        "topics": topics or ["math"],
    }


def _claim(
    claim_id: str,
    paper_id: str,
    *,
    statement: str = "Stub claim.",
    proof: str | None = None,
    replication_status: str = "untested",
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": claim_id,
        "paper_id": paper_id,
        "statement": statement,
        "claim_type": "theoretical",
        "evidence_type": "argument",
        "replication_status": replication_status,
    }
    if proof is not None:
        out["proof"] = proof
    if depends_on:
        out["depends_on"] = depends_on
    return out


def _client_with_orcid_bearer() -> tuple[Any, httpx.Client]:
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


# ---------------------------------------------------------------------------
# diff.py (RRP-0017)
# ---------------------------------------------------------------------------


def test_claim_local_id_parse() -> None:
    assert claim_local_id("01923f8e-5b2a-7c4d-9e1f-3a2b1c0d4e5f:c1") == "c1"
    # local_id may contain further colons.
    assert (
        claim_local_id("01923f8e-0009-7c4d-9e1f-3a2b1c0d4e5f:prop:I.10")
        == "prop:I.10"
    )
    assert claim_local_id("") is None
    assert claim_local_id("no-colon") is None


def test_papers_in_same_lineage() -> None:
    papers = {
        "p1": {"id": "p1", "previous_version": None},
        "p2": {"id": "p2", "previous_version": "p1"},
        "p3": {"id": "p3", "previous_version": "p2"},
        "q1": {"id": "q1", "previous_version": None},
    }
    assert papers_in_same_lineage(papers.get, "p1", "p3") is True
    assert papers_in_same_lineage(papers.get, "p3", "p1") is True
    assert papers_in_same_lineage(papers.get, "p2", "p2") is True
    assert papers_in_same_lineage(papers.get, "p1", "q1") is False


def test_compute_revision_diff_matches_by_local_id() -> None:
    """A claim with the same local_id across versions should *match* (not
    show up as remove+add) even though the global claim_id differs."""
    from rrxiv.models import CIR

    prev_paper = _paper("p1")
    prev_cir = CIR.model_validate(
        {
            **prev_paper,
            "claims": [
                _claim("p1:c1", "p1", statement="Original statement."),
                _claim("p1:c2", "p1", statement="Unchanged claim."),
            ],
        }
    )

    curr_paper = _paper("p2", version="v2", previous_version="p1")
    curr_cir = CIR.model_validate(
        {
            **curr_paper,
            "claims": [
                _claim("p2:c1", "p2", statement="Revised statement."),
                _claim("p2:c2", "p2", statement="Unchanged claim."),
            ],
        }
    )

    diff = compute_revision_diff(prev_paper, prev_cir, curr_paper, curr_cir)
    assert diff["from"]["paper_id"] == "p1"
    assert diff["to"]["paper_id"] == "p2"
    assert diff["claims"]["added"] == []
    assert diff["claims"]["removed"] == []
    assert diff["claims"]["unchanged_count"] == 1
    assert len(diff["claims"]["modified"]) == 1

    mod = diff["claims"]["modified"][0]
    assert mod["local_id"] == "c1"
    assert mod["from_claim_id"] == "p1:c1"
    assert mod["to_claim_id"] == "p2:c1"
    assert "statement" in mod["fields_changed"]
    assert mod["statement_diff"]["hunks"]


def test_compute_revision_diff_added_and_removed_claims() -> None:
    from rrxiv.models import CIR

    prev_paper = _paper("p1")
    prev_cir = CIR.model_validate(
        {
            **prev_paper,
            "claims": [
                _claim("p1:c1", "p1"),
                _claim("p1:c-gone", "p1"),
            ],
        }
    )

    curr_paper = _paper("p2", version="v2", previous_version="p1")
    curr_cir = CIR.model_validate(
        {
            **curr_paper,
            "claims": [
                _claim("p2:c1", "p2"),  # matches by local_id
                _claim("p2:c-new", "p2", statement="brand new claim"),
            ],
        }
    )

    diff = compute_revision_diff(prev_paper, prev_cir, curr_paper, curr_cir)
    added_ids = {c["claim_id"] for c in diff["claims"]["added"]}
    removed_ids = {c["claim_id"] for c in diff["claims"]["removed"]}
    assert added_ids == {"p2:c-new"}
    assert removed_ids == {"p1:c-gone"}


# ---------------------------------------------------------------------------
# Diff endpoint (RRP-0017)
# ---------------------------------------------------------------------------


def test_diff_endpoint_happy_path() -> None:
    """End-to-end: seed v1 + v2, hit /papers/{v2}/diff?from={v1}."""
    app, sync = _client_with_orcid_bearer()
    store = app.state.store

    v1 = _paper("p-v1")
    v2 = _paper("p-v2", version="v2", previous_version="p-v1", abstract="updated")
    store.add_paper({k: v for k, v in v1.items() if k != "rrxiv_version"})
    store.add_paper({k: v for k, v in v2.items() if k != "rrxiv_version"})
    store.add_cir({**v1, "claims": [_claim("p-v1:c1", "p-v1")]})
    store.add_cir({**v2, "claims": [_claim("p-v2:c1", "p-v2", statement="Different statement.")]})

    resp = sync.get("/papers/p-v2/diff", params={"from": "p-v1"})
    sync.close()
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["from"]["paper_id"] == "p-v1"
    assert body["to"]["paper_id"] == "p-v2"
    assert body["abstract_changed"] is True
    assert body["claims"]["modified"], "expected a modified claim"


def test_diff_endpoint_lineage_mismatch_returns_400() -> None:
    app, sync = _client_with_orcid_bearer()
    store = app.state.store
    store.add_paper({k: v for k, v in _paper("p-a").items() if k != "rrxiv_version"})
    store.add_paper({k: v for k, v in _paper("p-b").items() if k != "rrxiv_version"})

    resp = sync.get("/papers/p-a/diff", params={"from": "p-b"})
    sync.close()
    assert resp.status_code == 400
    body = resp.json()
    # FastAPI's HTTPException puts our dict under 'detail'.
    assert body["detail"]["code"] == "papers_not_in_same_lineage"


# ---------------------------------------------------------------------------
# Errata endpoint (RRP-0017 companion)
# ---------------------------------------------------------------------------


def test_errata_endpoint_returns_only_errata_for_paper() -> None:
    app, sync = _client_with_orcid_bearer()
    store = app.state.store
    store.add_paper({k: v for k, v in _paper("p-err").items() if k != "rrxiv_version"})

    # Two errata, one comment — only errata should come back.
    store.add_annotation(
        {
            "id": "ann-erratum-1",
            "target_id": "p-err",
            "target_type": "paper",
            "annotation_type": "erratum",
            "content": "typo in §3",
            "created_at": "2026-05-01T00:00:00Z",
            "created_by": {"identity_type": "orcid", "identity": "0000-0001-0000-0001"},
        }
    )
    store.add_annotation(
        {
            "id": "ann-comment-1",
            "target_id": "p-err",
            "target_type": "paper",
            "annotation_type": "comment",
            "content": "nice paper",
            "created_at": "2026-05-02T00:00:00Z",
            "created_by": {"identity_type": "orcid", "identity": "0000-0001-0000-0002"},
        }
    )
    store.add_annotation(
        {
            "id": "ann-erratum-2",
            "target_id": "p-err",
            "target_type": "paper",
            "annotation_type": "erratum",
            "content": "missing reference",
            "created_at": "2026-05-03T00:00:00Z",
            "created_by": {"identity_type": "orcid", "identity": "0000-0001-0000-0003"},
        }
    )

    resp = sync.get("/papers/p-err/errata")
    sync.close()
    assert resp.status_code == 200
    body = resp.json()
    types = [a["annotation_type"] for a in body["items"]]
    assert types == ["erratum", "erratum"]
    # Newest first.
    assert body["items"][0]["id"] == "ann-erratum-2"


# ---------------------------------------------------------------------------
# Annotation threads (RRP-0018)
# ---------------------------------------------------------------------------


def _post_annotation(sync: httpx.Client, body: dict[str, Any]) -> httpx.Response:
    return sync.post("/annotations", json=body)


def test_in_reply_to_validates_existence() -> None:
    app, sync = _client_with_orcid_bearer()
    store = app.state.store
    store.add_paper({k: v for k, v in _paper("p-t").items() if k != "rrxiv_version"})

    resp = _post_annotation(
        sync,
        {
            "id": "ann-orphan-reply",
            "target_id": "p-t",
            "target_type": "paper",
            "annotation_type": "comment",
            "content": "reply to a non-existent annotation",
            "in_reply_to": "ann-does-not-exist",
            "created_at": "2026-05-01T00:00:00Z",
            "created_by": {"identity_type": "orcid", "identity": "0000-0001-0000-0001"},
        },
    )
    sync.close()
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "in_reply_to_not_found"


def test_in_reply_to_validates_same_artefact() -> None:
    app, sync = _client_with_orcid_bearer()
    store = app.state.store
    store.add_paper({k: v for k, v in _paper("p-a").items() if k != "rrxiv_version"})
    store.add_paper({k: v for k, v in _paper("p-b").items() if k != "rrxiv_version"})

    # Parent annotation on paper p-a.
    store.add_annotation(
        {
            "id": "ann-parent",
            "target_id": "p-a",
            "target_type": "paper",
            "annotation_type": "comment",
            "content": "parent on p-a",
            "created_at": "2026-05-01T00:00:00Z",
            "created_by": {"identity_type": "orcid", "identity": "0000-0001-0000-0001"},
        }
    )

    # Reply on paper p-b — should be rejected.
    resp = _post_annotation(
        sync,
        {
            "id": "ann-cross-reply",
            "target_id": "p-b",
            "target_type": "paper",
            "annotation_type": "comment",
            "content": "reply on a different paper",
            "in_reply_to": "ann-parent",
            "created_at": "2026-05-02T00:00:00Z",
            "created_by": {"identity_type": "orcid", "identity": "0000-0001-0000-0001"},
        },
    )
    sync.close()
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "in_reply_to_artefact_mismatch"


def test_in_reply_to_happy_path_and_replies_endpoint() -> None:
    app, sync = _client_with_orcid_bearer()
    store = app.state.store
    store.add_paper({k: v for k, v in _paper("p-thread").items() if k != "rrxiv_version"})

    store.add_annotation(
        {
            "id": "ann-root",
            "target_id": "p-thread",
            "target_type": "paper",
            "annotation_type": "comment",
            "content": "thread root",
            "created_at": "2026-05-01T00:00:00Z",
            "created_by": {"identity_type": "orcid", "identity": "0000-0001-0000-0001"},
        }
    )

    resp = _post_annotation(
        sync,
        {
            "id": "ann-reply-1",
            "target_id": "p-thread",
            "target_type": "paper",
            "annotation_type": "comment",
            "content": "first reply",
            "in_reply_to": "ann-root",
            "created_at": "2026-05-02T00:00:00Z",
            "created_by": {"identity_type": "orcid", "identity": "0000-0001-0000-0002"},
        },
    )
    assert resp.status_code == 201, resp.text

    replies = sync.get("/annotations/ann-root/replies")
    sync.close()
    assert replies.status_code == 200
    body = replies.json()
    assert [a["id"] for a in body["items"]] == ["ann-reply-1"]


# ---------------------------------------------------------------------------
# Replication status derivation (RRP-0019 + RRP-0020)
# ---------------------------------------------------------------------------


def test_derive_status_untested_when_no_annotations_and_no_authored() -> None:
    store = MemoryStore()
    store.add_claim(_claim("p1:c1", "p1"))
    assert derive_replication_status("p1:c1", store) == "untested"


def test_derive_status_falls_back_to_authored_value() -> None:
    """V0.x compromise: with zero replication annotations, the persisted
    `replication_status` is honoured (Euclid corpus stays working)."""
    store = MemoryStore()
    assert (
        derive_replication_status(
            "p1:c1", store, authored_default="replicated"
        )
        == "replicated"
    )


def test_derive_status_replicated_via_quorum() -> None:
    store = MemoryStore()
    # math quorum is 1.
    store.add_paper({"id": "p1", "topics": ["math"]})
    for i, _who in enumerate(["a", "b"]):
        store.add_annotation(
            {
                "id": f"ann-rep-{i}",
                "target_id": "p1:c1",
                "target_type": "claim",
                "annotation_type": "replication",
                "content": f"independent replication {i}",
                "structured_payload": {
                    "outcome": "supports",
                    "reproduction_kind": "fresh_replication",
                    "method": "by hand",
                },
                "created_at": f"2026-05-0{i+1}T00:00:00Z",
                "created_by": {"identity_type": "orcid", "identity": f"0000-0001-0000-000{i+1}"},
            }
        )
    assert derive_replication_status("p1:c1", store) == "replicated"


def test_derive_status_contradicted() -> None:
    store = MemoryStore()
    store.add_paper({"id": "p1", "topics": ["math"]})
    store.add_annotation(
        {
            "id": "ann-c",
            "target_id": "p1:c1",
            "target_type": "claim",
            "annotation_type": "replication",
            "content": "counter-example",
            "structured_payload": {
                "outcome": "contradicts",
                "reproduction_kind": "fresh_replication",
                "method": "counter-example construction",
            },
            "created_at": "2026-05-01T00:00:00Z",
            "created_by": {"identity_type": "orcid", "identity": "0000-0001-0000-000a"},
        }
    )
    assert derive_replication_status("p1:c1", store) == "contradicted"


def test_derive_status_retracted_by_claim_retraction() -> None:
    store = MemoryStore()
    store.add_paper({"id": "p1", "topics": ["math"]})
    # Replication says supports — but retraction wins.
    store.add_annotation(
        {
            "id": "ann-rep",
            "target_id": "p1:c1",
            "target_type": "claim",
            "annotation_type": "replication",
            "content": "supports",
            "structured_payload": {
                "outcome": "supports",
                "reproduction_kind": "fresh_replication",
                "method": "manual verification",
            },
            "created_at": "2026-05-01T00:00:00Z",
            "created_by": {"identity_type": "orcid", "identity": "0000-0001-0000-000a"},
        }
    )
    store.add_annotation(
        {
            "id": "ann-retract",
            "target_id": "p1:c1",
            "target_type": "claim",
            "annotation_type": "claim_retraction",
            "content": "author retracting due to bug in proof",
            "structured_payload": {
                "reason": (
                    "Found an off-by-one in the proof; "
                    "superseded by v2's corrected statement."
                ),
                "kind": "superseded_by_revision",
                "recommended_action": "use_v2",
            },
            "created_at": "2026-05-02T00:00:00Z",
            "created_by": {"identity_type": "orcid", "identity": "0000-0001-2345-6789"},
        }
    )
    assert derive_replication_status("p1:c1", store) == "retracted"


def test_retraction_can_be_lifted() -> None:
    """RRP-0020: retraction lifted by author's later comment with
    lifts_retraction=true reverts to normal derivation."""
    store = MemoryStore()
    store.add_paper({"id": "p1", "topics": ["math"]})
    author = {"identity_type": "orcid", "identity": "0000-0001-2345-6789"}
    store.add_annotation(
        {
            "id": "ann-retract",
            "target_id": "p1:c1",
            "target_type": "claim",
            "annotation_type": "claim_retraction",
            "content": "retracting",
            "structured_payload": {
                # Sprint 19.P1 tightened the payload: reason is a constrained
                # enum (data_error / methodological_flaw / fraud /
                # contamination / withdrawn_by_author / superseded_by_revision)
                # and the free-form rationale moves to `explanation`.
                "reason": "data_error",
                "explanation": "I thought there was a bug, more than 32 chars now.",
            },
            "created_at": "2026-05-01T00:00:00Z",
            "created_by": author,
        }
    )
    store.add_annotation(
        {
            "id": "ann-lift",
            "target_id": "p1:c1",
            "target_type": "claim",
            "annotation_type": "comment",
            "content": "on reflection the claim holds; lifting retraction",
            "in_reply_to": "ann-retract",
            "structured_payload": {
                "lifts_retraction": True,
                "reason": "claim holds after further review",
            },
            "created_at": "2026-05-02T00:00:00Z",
            "created_by": author,
        }
    )
    assert derive_replication_status("p1:c1", store) == "untested"


def test_quorum_per_discipline() -> None:
    store = MemoryStore()
    store.add_paper({"id": "p-math", "topics": ["math"]})
    store.add_paper({"id": "p-ml", "topics": ["ml"]})
    store.add_paper({"id": "p-psych", "topics": ["psychology"]})
    assert quorum_for_claim("p-math:c1", store) == 1
    assert quorum_for_claim("p-ml:c1", store) == 3
    assert quorum_for_claim("p-psych:c1", store) == 5
    # Unknown tag → default 3.
    store.add_paper({"id": "p-x", "topics": ["something-unknown"]})
    assert quorum_for_claim("p-x:c1", store) == 3


# ---------------------------------------------------------------------------
# Submission router updates (RRP-0016 + RRP-0017)
# ---------------------------------------------------------------------------


def _submit(
    sync: httpx.Client,
    cir: dict[str, Any],
    bundle_bytes: bytes,
    **form: Any,
) -> httpx.Response:
    files = {
        "cir": ("cir.json", json.dumps(cir).encode("utf-8"), "application/json"),
        "bundle": ("paper.tar.gz", bundle_bytes, "application/gzip"),
    }
    data = {k: str(v) for k, v in form.items() if v is not None}
    return sync.post("/submissions", files=files, data=data)


def test_submit_dry_run_does_not_persist() -> None:
    app, sync = _client_with_orcid_bearer()
    cir = _paper("p-dryrun")
    resp = _submit(sync, cir, b"bundle bytes", dry_run="true")
    sync.close()
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    assert body["would_persist"] is True
    assert body["paper_id"] is None
    # Nothing landed in the store.
    assert app.state.store.get_paper("p-dryrun") is None


def test_submit_bundle_hash_mismatch_is_400() -> None:
    _app, sync = _client_with_orcid_bearer()
    cir = _paper("p-bh")
    bundle_bytes = b"the real bytes"
    wrong_hash = hashlib.sha256(b"different bytes").hexdigest()
    resp = _submit(
        sync, cir, bundle_bytes, client_compile_hash=wrong_hash
    )
    sync.close()
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "bundle_hash_mismatch"


def test_submit_revision_attaches_diff_and_synthesises_summary() -> None:
    """End-to-end: submit v1, submit v2 with previous_version +
    revision_summary; response has revision_diff inline; the store gains
    a revision_summary annotation on v2."""
    app, sync = _client_with_orcid_bearer()
    v1 = _paper("p-rev-v1")
    v1["claims"] = [_claim("p-rev-v1:c1", "p-rev-v1", statement="Original.")]
    resp = _submit(sync, v1, b"v1 bundle")
    assert resp.status_code == 201, resp.text

    v2 = _paper("p-rev-v2", version="v2", previous_version="p-rev-v1", abstract="v2 abstract")
    v2["claims"] = [
        _claim("p-rev-v2:c1", "p-rev-v2", statement="Revised statement.")
    ]
    resp2 = _submit(
        sync,
        v2,
        b"v2 bundle",
        previous_version="p-rev-v1",
        revision_summary="Revised claim 1 to use a sharper bound.",
    )
    sync.close()
    assert resp2.status_code == 201, resp2.text
    body = resp2.json()
    assert body["version"] == "v2"
    assert body["previous_version"] == "p-rev-v1"
    assert body["revision_diff"] is not None
    assert body["revision_diff"]["abstract_changed"] is True

    # revision_summary annotation should now exist on the v2 paper.
    summaries = [
        a
        for a in app.state.store.list_annotations()
        if a.get("annotation_type") == "revision_summary"
        and a.get("target_id") == "p-rev-v2"
    ]
    assert len(summaries) == 1
    assert summaries[0]["structured_payload"]["previous_version_id"] == "p-rev-v1"


def test_submit_unknown_previous_version_is_400() -> None:
    _app, sync = _client_with_orcid_bearer()
    cir = _paper("p-bad-rev", version="v2", previous_version="paper-does-not-exist")
    resp = _submit(sync, cir, b"bundle", previous_version="paper-does-not-exist")
    sync.close()
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "previous_version_not_found"


def test_submit_revision_with_same_cir_id_mints_fresh_paper_id() -> None:
    """Regression: a CIR whose ``id`` matches ``previous_version`` would
    overwrite the predecessor + introduce a self-loop in the lineage
    (paper.id == paper.previous_version). The server now mints a fresh
    paper_id in this case so each version gets a distinct row.
    """
    app, sync = _client_with_orcid_bearer()

    v1 = _paper("collision-v1")
    v1["claims"] = [_claim("collision-v1:c1", "collision-v1")]
    resp1 = _submit(sync, v1, b"v1 bundle")
    assert resp1.status_code == 201, resp1.text

    # CIR for v2 carries the SAME id as v1 (a re-submit of the same
    # paper repo where \rrxivid{} didn't change).
    v2 = _paper("collision-v1", version="v2", previous_version="collision-v1")
    v2["claims"] = [_claim("collision-v1:c1", "collision-v1", statement="Updated.")]
    resp2 = _submit(
        sync,
        v2,
        b"v2 bundle",
        previous_version="collision-v1",
    )
    sync.close()
    assert resp2.status_code == 201, resp2.text
    body = resp2.json()
    assert body["paper_id"] != "collision-v1", (
        "server must mint a fresh paper_id when CIR's id collides with "
        "previous_version, not overwrite v1 + create a self-loop"
    )
    assert body["previous_version"] == "collision-v1"
    # Both rows must still exist in the store.
    assert app.state.store.get_paper("collision-v1") is not None
    assert app.state.store.get_paper(body["paper_id"]) is not None


def test_submit_persists_claims_into_claims_table() -> None:
    """Regression: claims embedded in the submitted CIR must be persisted
    as individual rows in the store's claims table so the
    ``/papers/{id}/claims`` + ``/claims/{id}`` read paths find them.

    Earlier the submit handler stored only the paper metadata + the
    CIR blob; ``/papers/{id}/claims`` returned an empty list for any
    paper that came through ``/submissions`` (vs the seed-store flow
    which did persist claims). Surfaced live on whitepaper v3 — the
    home page showed ``0 claims`` for an otherwise-valid paper.
    """
    app, sync = _client_with_orcid_bearer()

    paper = _paper("p-claims-persisted")
    paper["claims"] = [
        _claim("p-claims-persisted:c1", "p-claims-persisted", statement="First claim."),
        _claim("p-claims-persisted:c2", "p-claims-persisted", statement="Second claim."),
        _claim("p-claims-persisted:c3", "p-claims-persisted", statement="Third claim."),
    ]
    resp = _submit(sync, paper, b"bundle")
    assert resp.status_code == 201, resp.text

    # Store must now have the three claim rows individually.
    stored_claims = [
        c for c in app.state.store.list_claims()
        if c.get("paper_id") == "p-claims-persisted"
    ]
    assert len(stored_claims) == 3, stored_claims

    # And the public read paths must surface them.
    list_resp = sync.get("/papers/p-claims-persisted/claims")
    sync.close()
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    assert len(items) == 3
    assert {c["id"] for c in items} == {
        "p-claims-persisted:c1",
        "p-claims-persisted:c2",
        "p-claims-persisted:c3",
    }


def test_paper_versions_endpoint_cycle_safe() -> None:
    """A pathological self-loop (paper.id == paper.previous_version)
    in the store must not hang the versions walker — earlier code
    looped forever and 502'd the endpoint.
    """
    app, sync = _client_with_orcid_bearer()

    selfloop = _paper("selfloop")
    selfloop["previous_version"] = "selfloop"
    app.state.store.add_paper(selfloop)

    resp = sync.get("/papers/selfloop/versions")
    sync.close()
    assert resp.status_code == 200
    chain = resp.json()["items"]
    assert len(chain) == 1
    assert chain[0]["id"] == "selfloop"
