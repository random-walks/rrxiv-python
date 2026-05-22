"""Papers router — list, detail, related, claims-for-paper.

Read endpoints. Public, no auth required (the protocol's posture: free
read access, auth-gated writes). Returns the list-item projection
(RRP-0012) on the list endpoint and on the detail endpoint when the
``?include=stats`` query param is set.

Paper IDs are accepted in two forms (RRP-0013):
  - Canonical UUIDv7: ``01923f8e-5b2a-7c4d-9e1f-3a2b1c0d4e5f``
  - Human slug:        ``rrxiv:2402.00128``
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from rrxiv.models import CIR
from rrxiv.server.deps import get_store
from rrxiv.server.errors import not_found
from rrxiv.server.pagination import paginate
from rrxiv.server.papers.diff import compute_revision_diff, papers_in_same_lineage
from rrxiv.server.papers.projection import compute_stats, to_list_item
from rrxiv.server.papers.scopes import filter_by_scope
from rrxiv.server.papers.slug import find_paper_by_slug, is_slug
from rrxiv.server.store import Store

router = APIRouter(prefix="/papers", tags=["Papers"])


def _resolve_paper(store: Store, ident: str) -> dict[str, Any] | None:
    """Look up a paper by canonical id or by id_slug."""
    if is_slug(ident):
        return find_paper_by_slug(store, ident)
    return store.get_paper(ident)


@router.get("")
def list_papers(
    request: Request,
    scope: str | None = Query(
        None,
        description="Filter by UI-discovery scope. Known values: "
        "active/agent/human/contested/fresh/all (see GET /scopes).",
    ),
    topic: str | None = Query(
        None,
        description="Filter to papers whose `topics[]` contains this string.",
    ),
    cursor: str | None = Query(
        None,
        description="Opaque pagination cursor (RRP-0014).",
    ),
    limit: int | None = Query(
        None,
        ge=1,
        le=200,
        description="Maximum items per page. Defaults to 50.",
    ),
    include_superseded: bool = Query(
        False,
        description=(
            "Include older versions in the listing. Defaults to False — "
            "the listing returns one row per lineage (the head, i.e. the "
            "latest version per RRP-0017 ``previous_version`` chain). "
            "Set to True for archival / audit views that need every "
            "version individually."
        ),
    ),
) -> dict[str, Any]:
    store: Store = get_store(request)
    all_papers = list(store.list_papers())

    if not include_superseded:
        # Drop papers that are pointed to by another paper's
        # `previous_version` — they're an older version of something
        # else in the corpus. Per RRP-0017 the lineage forms a chain,
        # so this removes exactly the non-head rows.
        superseded_ids: set[str] = set()
        for p in all_papers:
            prev = p.get("previous_version")
            if isinstance(prev, str) and prev:
                superseded_ids.add(prev)
        all_papers = [p for p in all_papers if p.get("id") not in superseded_ids]

    items = [to_list_item(p, store) for p in all_papers]

    if topic:
        items = [
            item for item in items if topic in (item.get("topics") or [])
        ]
    if scope:
        items = filter_by_scope(items, scope)

    page, next_cursor = paginate(
        items,
        cursor=cursor,
        limit=limit,
        key=lambda p: (p.get("submitted_at") or "", p.get("id") or ""),
        order="desc",
    )
    return {"items": page, "next_cursor": next_cursor}


@router.get("/{paper_id}")
def get_paper(
    paper_id: str,
    request: Request,
    include: str | None = Query(
        None,
        description="Comma-separated extras. 'stats' → include the list-item stats projection.",
    ),
) -> dict[str, Any]:
    store: Store = get_store(request)
    paper = _resolve_paper(store, paper_id)
    if paper is None:
        raise not_found(f"paper {paper_id} not found")
    includes = set((include or "").split(",")) if include else set()
    if "stats" in includes:
        return to_list_item(paper, store)
    return paper


@router.get("/{paper_id}/cir")
def get_cir(paper_id: str, request: Request) -> dict[str, Any]:
    store: Store = get_store(request)
    paper = _resolve_paper(store, paper_id)
    if paper is None:
        raise not_found(f"paper {paper_id} not found")
    canonical_id = paper["id"]
    cir = store.get_cir(canonical_id)
    if cir is None:
        # Fall back to the metadata-only view + empty annotations,
        # which the existing MockRrxivServer also does.
        cir = dict(paper)
        cir.setdefault("annotations", [])
    return cir


@router.get("/{paper_id}/claims")
def list_claims_for_paper(
    paper_id: str, request: Request
) -> dict[str, Any]:
    """Claims registered on the given paper."""
    store: Store = get_store(request)
    paper = _resolve_paper(store, paper_id)
    if paper is None:
        raise not_found(f"paper {paper_id} not found")
    canonical_id = paper["id"]
    items = [
        c for c in store.list_claims() if c.get("paper_id") == canonical_id
    ]
    return {"items": items, "next_cursor": None}


@router.get("/{paper_id}/related")
def list_related_papers(
    paper_id: str,
    request: Request,
    limit: int = Query(3, ge=1, le=20),
) -> dict[str, Any]:
    """Topic-overlap (Jaccard) related papers. v0.1 implementation —
    citation-graph traversal is a future enhancement."""
    store: Store = get_store(request)
    target = _resolve_paper(store, paper_id)
    if target is None:
        raise not_found(f"paper {paper_id} not found")
    canonical_id = target["id"]
    target_topics = set(target.get("topics") or [])

    scored: list[tuple[float, dict[str, Any]]] = []
    for other in store.list_papers():
        if other["id"] == canonical_id:
            continue
        other_topics = set(other.get("topics") or [])
        union = target_topics | other_topics
        if not union:
            continue
        score = len(target_topics & other_topics) / len(union)
        if score > 0:
            scored.append((score, other))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    items = [to_list_item(p, store) for _, p in scored[:limit]]
    return {"items": items, "next_cursor": None}


# ---------- Stats convenience endpoint -------------------------------


@router.get("/{paper_id}/stats")
def get_paper_stats(paper_id: str, request: Request) -> dict[str, Any]:
    """Just the aggregate stats for a paper, without the Paper payload."""
    store: Store = get_store(request)
    paper = _resolve_paper(store, paper_id)
    if paper is None:
        raise not_found(f"paper {paper_id} not found")
    return compute_stats(paper["id"], store)


# ---------- Revision diff endpoint (RRP-0017) ------------------------


@router.get("/{paper_id}/diff")
def get_revision_diff(
    paper_id: str,
    request: Request,
    from_: str = Query(
        ...,
        alias="from",
        description="Paper ID (or slug) of the prior version.",
    ),
) -> dict[str, Any]:
    """Semantic diff between two versions of a paper (RRP-0017).

    Both papers must share a `previous_version` lineage. The `{paper_id}` path
    parameter is the newer paper; the `from` query parameter is the prior.

    The diff is deterministic — claims are matched by `local_id` first,
    then by exact statement. Word-level hunks accompany statement + proof
    changes. The output validates against
    ``schema/revision_diff.schema.json``.
    """
    store: Store = get_store(request)
    curr = _resolve_paper(store, paper_id)
    prev = _resolve_paper(store, from_)
    if curr is None:
        raise not_found(f"paper {paper_id} not found")
    if prev is None:
        raise not_found(f"paper {from_} not found")

    if not papers_in_same_lineage(store.get_paper, prev["id"], curr["id"]):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "papers_not_in_same_lineage",
                "message": (
                    f"papers {prev['id']} and {curr['id']} do not share a "
                    "`previous_version` chain; cannot compute a revision diff"
                ),
            },
        )

    prev_cir_raw = store.get_cir(prev["id"]) or dict(prev)
    curr_cir_raw = store.get_cir(curr["id"]) or dict(curr)
    prev_cir = CIR.model_validate(prev_cir_raw)
    curr_cir = CIR.model_validate(curr_cir_raw)

    return compute_revision_diff(prev, prev_cir, curr, curr_cir)


# ---------- Errata listing (RRP-0017) --------------------------------


@router.get("/{paper_id}/errata")
def list_errata(
    paper_id: str,
    request: Request,
    cursor: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    """List erratum annotations for this paper, newest first.

    Convenience over filtering /annotations by target + type. Paginated
    via cursor.
    """
    store: Store = get_store(request)
    paper = _resolve_paper(store, paper_id)
    if paper is None:
        raise not_found(f"paper {paper_id} not found")
    canonical_id = paper["id"]

    matches = [
        a
        for a in store.list_annotations()
        if a.get("annotation_type") == "erratum"
        and (
            a.get("target_id") == canonical_id
            or str(a.get("target_id", "")).startswith(f"{canonical_id}:")
        )
    ]
    items, next_cursor = paginate(
        matches,
        cursor=cursor,
        limit=limit,
        key=lambda a: (a.get("created_at") or "", a.get("id") or ""),
        order="desc",
    )
    return {"items": items, "next_cursor": next_cursor}
