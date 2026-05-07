"""Search router — naive substring matching over the in-memory store.

A real deployment swaps this for a proper search backend (e.g.,
Postgres FTS, Tantivy, Meilisearch). The reference server uses a
linear scan because its job is to expose the *wire format*, not to
be fast at scale.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from rrxiv.server.deps import get_store
from rrxiv.server.errors import bad_request
from rrxiv.server.store import Store

router = APIRouter(prefix="/search", tags=["Search"])


@router.get("/papers")
def search_papers(
    request: Request,
    q: str = Query(..., min_length=1),
    limit: int = Query(default=20, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> dict[str, Any]:
    if not q.strip():
        raise bad_request("query is empty")
    store: Store = get_store(request)
    needle = q.lower()
    matches: list[dict[str, Any]] = []
    for paper in store.list_papers():
        if _paper_matches(paper, needle):
            matches.append(paper)
            if len(matches) >= limit:
                break
    return {"items": matches, "next_cursor": None}


@router.get("/claims")
def search_claims(
    request: Request,
    q: str = Query(..., min_length=1),
    limit: int = Query(default=20, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> dict[str, Any]:
    if not q.strip():
        raise bad_request("query is empty")
    store: Store = get_store(request)
    needle = q.lower()
    matches: list[dict[str, Any]] = []
    for claim in store.list_claims():
        statement = (claim.get("statement") or "").lower()
        if needle in statement:
            matches.append(claim)
            if len(matches) >= limit:
                break
    return {"items": matches, "next_cursor": None}


def _paper_matches(paper: dict[str, Any], needle: str) -> bool:
    for field_name in ("title", "abstract"):
        v = paper.get(field_name) or ""
        if needle in str(v).lower():
            return True
    for author in paper.get("authors") or []:
        name = author.get("name") if isinstance(author, dict) else str(author)
        if name and needle in name.lower():
            return True
    for topic in paper.get("topics") or []:
        if needle in str(topic).lower():
            return True
    return False
