"""Discovery router — GET /scopes, GET /topics, GET /stats."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request

from rrxiv.server.deps import get_store
from rrxiv.server.papers.scopes import SCOPES
from rrxiv.server.store import Store

router = APIRouter(tags=["Discovery"])


@router.get("/scopes")
def list_scopes(request: Request) -> dict[str, Any]:
    """UI-discovery scopes published by this instance.

    Instance metadata, NOT protocol-binding. Other rrxiv instances may
    expose different scopes; clients read this endpoint rather than
    hardcoding a set.
    """
    return {"items": SCOPES, "next_cursor": None}


@router.get("/topics")
def list_topics(request: Request) -> dict[str, Any]:
    """Sorted unique list of topics present in the corpus.

    Derived from ``paper.topics[]`` across all papers. Useful for
    populating UI facets.
    """
    store: Store = get_store(request)
    topics: set[str] = set()
    for paper in store.list_papers():
        for topic in paper.get("topics") or []:
            if isinstance(topic, str) and topic:
                topics.add(topic)
    items = sorted(topics)
    return {"items": items, "next_cursor": None}


@router.get("/stats")
def corpus_stats(request: Request) -> dict[str, Any]:
    """Aggregate corpus counts. Computed live; cheap for v0.1 corpora,
    will need memoisation at scale."""
    store: Store = get_store(request)
    papers = store.list_papers()
    claims = store.list_claims()
    annotations = store.list_annotations()

    replications = sum(
        1
        for a in annotations
        if a.get("annotation_type") == "replication"
    )
    retractions = sum(
        1
        for a in annotations
        if a.get("annotation_type") == "erratum"
        and isinstance(a.get("structured_payload"), dict)
        and a["structured_payload"].get("retracted") is True
    )
    contradictions = sum(
        1
        for a in annotations
        if a.get("annotation_type") == "contradiction"
    )
    agent_authored_paper_count = sum(
        1
        for p in papers
        for author in (p.get("authors") or [])
        if isinstance(author, dict) and author.get("is_agent")
    )
    # An "active study" for v0.1 = a paper whose stats.status would be
    # `untested` with >4 claims, i.e. matches the "active" scope.
    # We don't recompute stats per-paper here; this is intentionally a
    # cheap heuristic that approximates the home-page corpus card.
    active_studies = sum(
        1
        for p in papers
        if any(
            c.get("paper_id") == p["id"]
            and c.get("replication_status") in (None, "untested")
            for c in claims
        )
        and sum(1 for c in claims if c.get("paper_id") == p["id"]) > 4
    )

    return {
        "papers": len(papers),
        "claims": len(claims),
        "annotations": len(annotations),
        "replications": replications,
        "contradictions": contradictions,
        "retractions": retractions,
        "active_studies": active_studies,
        "agent_authored_papers": agent_authored_paper_count,
        "computed_at": (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        ),
    }
