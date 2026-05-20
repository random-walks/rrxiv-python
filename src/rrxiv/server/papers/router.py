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

from fastapi import APIRouter, Query, Request

from rrxiv.server.deps import get_store
from rrxiv.server.errors import not_found
from rrxiv.server.pagination import paginate
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
) -> dict[str, Any]:
    store: Store = get_store(request)
    items = [to_list_item(p, store) for p in store.list_papers()]

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
