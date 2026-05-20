#!/usr/bin/env python3
"""Apply discourse-layer enrichments to the seed CIRs.

Idempotent — every annotation, citation, and claim edge has a stable ID
so re-runs replace existing entries with the same id rather than
duplicating. Run after editing this file (the data definitions live
inline below) to regenerate seed/*.cir.json with the enrichments
applied.

Usage::

    uv run python scripts/enrich-seed.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SEED_DIR = Path(__file__).resolve().parents[1] / "seed"

# Mapping of slug → CIR file name.
PAPERS: dict[str, str] = {
    "rrxiv:2605.00001": "rrxiv-whitepaper.cir.json",
    "rrxiv:2605.00002": "claim-graph-first-class.cir.json",
    "rrxiv:2605.00003": "reproducibility-budgets.cir.json",
    "rrxiv:2605.00004": "shrinkage-estimators.cir.json",
    "rrxiv:2605.00005": "agents-as-editors.cir.json",
    "rrxiv:2605.00006": "citation-vs-knowledge-graphs.cir.json",
    "rrxiv:2605.00007": "retraction-as-data.cir.json",
    "rrxiv:2605.00008": "active-replication.cir.json",
}


def _load(name: str) -> dict[str, Any]:
    with (SEED_DIR / name).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _save(name: str, cir: dict[str, Any]) -> None:
    with (SEED_DIR / name).open("w", encoding="utf-8") as fh:
        json.dump(cir, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def _paper_uuid(name: str) -> str:
    return _load(name)["id"]


def _claim_id(paper_uuid: str, n: int) -> str:
    return f"{paper_uuid}:c{n}"


def _upsert(items: list[Any], item: Any, key: str = "id") -> list[Any]:
    """Replace an entry with the same key, otherwise append."""
    item_key = item.get(key) if isinstance(item, dict) else item
    out: list[Any] = []
    replaced = False
    for existing in items:
        existing_key = existing.get(key) if isinstance(existing, dict) else existing
        if existing_key == item_key:
            out.append(item)
            replaced = True
        else:
            out.append(existing)
    if not replaced:
        out.append(item)
    return out


def _ensure_in_list(items: list[Any], value: Any) -> list[Any]:
    if value in items:
        return items
    return [*items, value]


def _attach_to_claim(cir: dict[str, Any], claim_index: int, field: str, value: str) -> None:
    """Add ``value`` to the named ref list on the indexed claim (1-based)."""
    claim = cir["claims"][claim_index - 1]
    claim[field] = _ensure_in_list(claim.get(field) or [], value)


def main() -> int:
    # Resolve UUIDs up front so we can reference them.
    uuid: dict[str, str] = {slug: _paper_uuid(name) for slug, name in PAPERS.items()}

    # --- Whitepaper ------------------------------------------------------
    name = PAPERS["rrxiv:2605.00001"]
    cir = _load(name)
    pid = uuid["rrxiv:2605.00001"]
    cir["citations"] = _upsert(
        cir.get("citations") or [],
        {
            "id": "cite-whitepaper-arxiv-1",
            "kind": "arxiv",
            "ref": "2305.12345",
            "title": "Open knowledge graphs for science",
        },
    )
    cir["citations"] = _upsert(
        cir["citations"],
        {
            "id": "cite-whitepaper-doi-1",
            "kind": "doi",
            "ref": "10.5281/zenodo.0000000",
            "title": "Replication studies in the open-science era",
        },
    )
    cir["annotations"] = _upsert(
        cir.get("annotations") or [],
        {
            "id": "ann-whitepaper-summary",
            "target_id": pid,
            "target_type": "paper",
            "annotation_type": "summary",
            "content": "Lays out the rrxiv protocol: append-only, claim-graph-first, human+agent coproduction.",
            "created_at": "2026-05-18T11:00:00Z",
            "created_by": {"identity_type": "agent", "identity": "agent:example-llm"},
        },
    )
    cir["annotations"] = _upsert(
        cir["annotations"],
        {
            "id": "ann-whitepaper-comment-1",
            "target_id": pid,
            "target_type": "paper",
            "annotation_type": "comment",
            "content": "Reviewers note: the governance commitment in §8 is the load-bearing claim.",
            "created_at": "2026-05-18T13:30:00Z",
            "created_by": {"identity_type": "orcid", "identity": "0000-0002-9999-0001"},
        },
    )
    _save(name, cir)

    # --- Claim graph first class ---------------------------------------
    name = PAPERS["rrxiv:2605.00002"]
    cir = _load(name)
    pid = uuid["rrxiv:2605.00002"]
    # Citations — paper cites arxiv on knowledge graphs
    for cite in (
        {"id": "cite-claimgraph-arxiv-1", "kind": "arxiv", "ref": "1607.07842", "title": "Survey of citation graphs"},
        {"id": "cite-claimgraph-arxiv-2", "kind": "arxiv", "ref": "1904.10501", "title": "Section embeddings for retrieval"},
        {"id": "cite-claimgraph-doi-1", "kind": "doi", "ref": "10.1145/3477123", "title": "Replication tracking at scale"},
    ):
        cir["citations"] = _upsert(cir.get("citations") or [], cite)
    # Annotations
    cir["annotations"] = _upsert(
        cir.get("annotations") or [],
        {
            "id": "ann-claimgraph-replication-1",
            "target_id": _claim_id(pid, 1),
            "target_type": "claim",
            "annotation_type": "replication",
            "content": "Group V independently replicated the 38% reduction on a 250-paper subset.",
            "evidence_links": ["https://example.org/replications/claimgraph-c1"],
            "created_at": "2026-05-18T12:00:00Z",
            "created_by": {"identity_type": "orcid", "identity": "0000-0001-7210-9999"},
            "structured_payload": {"effect_size": 0.38, "n": 250},
        },
    )
    cir["annotations"] = _upsert(
        cir["annotations"],
        {
            "id": "ann-claimgraph-comment-1",
            "target_id": pid,
            "target_type": "paper",
            "annotation_type": "comment",
            "content": "Useful framing — would value an explicit schema for retrieval evaluations.",
            "created_at": "2026-05-18T13:00:00Z",
            "created_by": {"identity_type": "orcid", "identity": "0000-0001-4444-2002"},
        },
    )
    _save(name, cir)

    # --- Reproducibility budgets ---------------------------------------
    name = PAPERS["rrxiv:2605.00003"]
    cir = _load(name)
    pid = uuid["rrxiv:2605.00003"]
    for cite in (
        {"id": "cite-repro-arxiv-1", "kind": "arxiv", "ref": "1812.07823", "title": "Computational reproducibility at scale"},
        {"id": "cite-repro-doi-1", "kind": "doi", "ref": "10.1038/s41586-021-03787-7", "title": "Reproducibility in machine learning"},
    ):
        cir["citations"] = _upsert(cir.get("citations") or [], cite)
    # Claim edges — this paper EXTENDS claimgraph c1
    _attach_to_claim(cir, 1, "extends", _claim_id(uuid["rrxiv:2605.00002"], 1))
    # Claim 2 supports claimgraph c4 (replication tracking)
    _attach_to_claim(cir, 2, "supports", _claim_id(uuid["rrxiv:2605.00002"], 4))
    cir["annotations"] = _upsert(
        cir.get("annotations") or [],
        {
            "id": "ann-repro-replication-1",
            "target_id": _claim_id(pid, 1),
            "target_type": "claim",
            "annotation_type": "replication",
            "content": "Replicated on an independent 18-paper corpus; effect size within CI.",
            "evidence_links": ["https://example.org/replications/repro-budgets-c1"],
            "created_at": "2026-05-18T14:00:00Z",
            "created_by": {"identity_type": "orcid", "identity": "0000-0003-1110-3001"},
            "structured_payload": {"n": 18, "effect_lower": 0.21, "effect_upper": 0.34},
        },
    )
    _save(name, cir)

    # --- Shrinkage estimators (contradicts) -----------------------------
    name = PAPERS["rrxiv:2605.00004"]
    cir = _load(name)
    pid = uuid["rrxiv:2605.00004"]
    for cite in (
        {"id": "cite-shrink-arxiv-1", "kind": "arxiv", "ref": "1908.04344", "title": "Hierarchical shrinkage for meta-analysis"},
    ):
        cir["citations"] = _upsert(cir.get("citations") or [], cite)
    # Claim edges — this paper contradicts a claimgraph claim
    _attach_to_claim(cir, 1, "contradicts", _claim_id(uuid["rrxiv:2605.00002"], 2))
    cir["annotations"] = _upsert(
        cir.get("annotations") or [],
        {
            "id": "ann-shrink-contradiction-1",
            "target_id": _claim_id(uuid["rrxiv:2605.00002"], 2),
            "target_type": "claim",
            "annotation_type": "contradiction",
            "content": "Under shrinkage, the section-vs-abstract retrieval gap shrinks below significance.",
            "evidence_links": ["https://example.org/contradictions/shrink-claimgraph-c2"],
            "created_at": "2026-05-18T14:30:00Z",
            "created_by": {"identity_type": "orcid", "identity": "0000-0002-5050-3001"},
        },
    )
    _save(name, cir)

    # --- Agents as editors ---------------------------------------------
    name = PAPERS["rrxiv:2605.00005"]
    cir = _load(name)
    pid = uuid["rrxiv:2605.00005"]
    for cite in (
        {"id": "cite-agents-arxiv-1", "kind": "arxiv", "ref": "2402.12345", "title": "Agent collaboration patterns for research"},
        {"id": "cite-agents-arxiv-2", "kind": "arxiv", "ref": "2403.04567", "title": "Editorial workflows with foundation models"},
    ):
        cir["citations"] = _upsert(cir.get("citations") or [], cite)
    _attach_to_claim(cir, 1, "depends_on", _claim_id(uuid["rrxiv:2605.00002"], 4))
    cir["annotations"] = _upsert(
        cir.get("annotations") or [],
        {
            "id": "ann-agents-summary",
            "target_id": pid,
            "target_type": "paper",
            "annotation_type": "summary",
            "content": "Argues for agents as first-class editorial actors: surfacing dissent, ranking claims, auditing replication.",
            "created_at": "2026-05-18T15:00:00Z",
            "created_by": {"identity_type": "agent", "identity": "agent:rrxiv-summariser"},
        },
    )
    _save(name, cir)

    # --- Citation vs knowledge graphs ----------------------------------
    name = PAPERS["rrxiv:2605.00006"]
    cir = _load(name)
    pid = uuid["rrxiv:2605.00006"]
    for cite in (
        {"id": "cite-citknow-arxiv-1", "kind": "arxiv", "ref": "1804.09301", "title": "Knowledge graphs from scientific abstracts"},
        {"id": "cite-citknow-doi-1", "kind": "doi", "ref": "10.1162/qss_a_00112", "title": "Citation networks vs knowledge graphs"},
    ):
        cir["citations"] = _upsert(cir.get("citations") or [], cite)
    _attach_to_claim(cir, 1, "depends_on", _claim_id(uuid["rrxiv:2605.00002"], 1))
    cir["annotations"] = _upsert(
        cir.get("annotations") or [],
        {
            "id": "ann-citknow-comment-1",
            "target_id": pid,
            "target_type": "paper",
            "annotation_type": "comment",
            "content": "The taxonomy in §3 maps cleanly onto the rrxiv claim graph.",
            "created_at": "2026-05-18T15:30:00Z",
            "created_by": {"identity_type": "orcid", "identity": "0000-0001-3333-4044"},
        },
    )
    _save(name, cir)

    # --- Retraction as data --------------------------------------------
    name = PAPERS["rrxiv:2605.00007"]
    cir = _load(name)
    pid = uuid["rrxiv:2605.00007"]
    for cite in (
        {"id": "cite-retract-doi-1", "kind": "doi", "ref": "10.1126/science.abc1234", "title": "The retraction record"},
    ):
        cir["citations"] = _upsert(cir.get("citations") or [], cite)
    cir["annotations"] = _upsert(
        cir.get("annotations") or [],
        {
            "id": "ann-retract-erratum-1",
            "target_id": pid,
            "target_type": "paper",
            "annotation_type": "erratum",
            "content": "Demonstration retraction (seed corpus); the historical record is preserved as required by the protocol.",
            "created_at": "2026-05-18T16:00:00Z",
            "created_by": {"identity_type": "orcid", "identity": "0000-0002-7777-0001"},
            "structured_payload": {"retracted": True, "reason": "demonstration"},
        },
    )
    _save(name, cir)

    # --- Active replication --------------------------------------------
    name = PAPERS["rrxiv:2605.00008"]
    cir = _load(name)
    pid = uuid["rrxiv:2605.00008"]
    for cite in (
        {"id": "cite-active-arxiv-1", "kind": "arxiv", "ref": "2105.10001", "title": "Discoverability metrics for preprints"},
        {"id": "cite-active-arxiv-2", "kind": "arxiv", "ref": "2210.13211", "title": "Cross-domain attention in scholarly networks"},
    ):
        cir["citations"] = _upsert(cir.get("citations") or [], cite)
    cir["annotations"] = _upsert(
        cir.get("annotations") or [],
        {
            "id": "ann-active-comment-1",
            "target_id": _claim_id(pid, 1),
            "target_type": "claim",
            "annotation_type": "comment",
            "content": "We have a replication pre-registered with our group; expected results by end of month.",
            "created_at": "2026-05-18T16:30:00Z",
            "created_by": {"identity_type": "orcid", "identity": "0000-0001-0000-0001"},
        },
    )
    _save(name, cir)

    print("enriched 8 seed CIRs (annotations + citations + claim edges)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
