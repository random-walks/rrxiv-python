"""Discovery router — GET /scopes, GET /topics, GET /stats."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query, Request

from rrxiv.server.deps import get_store
from rrxiv.server.papers.projection import compute_stats
from rrxiv.server.papers.scopes import SCOPES
from rrxiv.server.papers.slug import claim_owner_key
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
def list_topics(
    request: Request,
    with_counts: bool = Query(False, alias="with_counts"),
) -> dict[str, Any]:
    """Sorted unique list of topics present in the corpus.

    Derived from ``paper.topics[]`` across all papers. Useful for
    populating UI facets.

    When ``with_counts=1`` is passed, items become ``{topic, count}``
    objects sorted by count descending, then alphabetically.
    """
    store: Store = get_store(request)
    counter: Counter[str] = Counter()
    for paper in store.list_papers():
        for topic in paper.get("topics") or []:
            if isinstance(topic, str) and topic:
                counter[topic] += 1
    if with_counts:
        items: list[Any] = [
            {"topic": t, "count": c}
            for t, c in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
    else:
        items = sorted(counter.keys())
    return {"items": items, "next_cursor": None}


@router.get("/stats")
def corpus_stats(request: Request) -> dict[str, Any]:
    """Aggregate corpus counts. Computed live; cheap for v0.1 corpora,
    will need memoisation at scale.

    Reports two paper counts:

    - ``papers`` — total paper records, including superseded versions
      that no longer surface in head-of-lineage listings. Useful for
      "this corpus has 580 paper records on disk" stats.
    - ``head_papers`` — only the head-of-lineage rows (i.e. papers
      that aren't anyone else's ``previous_version``). Matches what
      ``GET /papers`` returns by default. This is the count clients
      should show in user-facing UI; the discrepancy was confusing
      on the home page ("Showing 9 of 20" with no Next button — the
      11 missing ones were superseded v1s).
    """
    store: Store = get_store(request)
    papers = store.list_papers()
    claims = store.list_claims()
    annotations = store.list_annotations()
    superseded_ids = {
        prev
        for p in papers
        if (prev := p.get("previous_version")) and isinstance(prev, str)
    }
    head_papers = sum(1 for p in papers if p.get("id") not in superseded_ids)

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

    by_status: Counter[str] = Counter()
    active_studies = 0
    for paper in papers:
        # Claims are slug-keyed (claim.paper_id == id_slug); use the
        # slug, not paper["id"] (the UUIDv7 PK). RRP-0013 / RRP-0029.
        owner_key = claim_owner_key(paper)
        stats = compute_stats(owner_key, store)
        by_status[stats["status"]] += 1
        paper_claims = [c for c in claims if c.get("paper_id") == owner_key]
        if stats["status"] == "untested" and len(paper_claims) > 4:
            active_studies += 1

    return {
        "papers": len(papers),
        "head_papers": head_papers,
        "claims": len(claims),
        "annotations": len(annotations),
        "replications": replications,
        "contradictions": contradictions,
        "retractions": retractions,
        "active_studies": active_studies,
        "agent_authored_papers": agent_authored_paper_count,
        "by_status": {
            "preprint": by_status.get("preprint", 0),
            "untested": by_status.get("untested", 0),
            "replicated": by_status.get("replicated", 0),
            "contested": by_status.get("contested", 0),
            "retracted": by_status.get("retracted", 0),
        },
        "computed_at": (
            datetime.now(UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        ),
    }
