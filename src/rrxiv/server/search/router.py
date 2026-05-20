"""Search router — naive substring matching over the in-memory store.

A real deployment swaps this for a proper search backend (e.g.,
Postgres FTS, Tantivy, Meilisearch). The reference server uses a
linear scan because its job is to expose the *wire format*, not to
be fast at scale.

GET /search/papers returns the PaperListItem shape (Paper + stats)
matching the rest of the read API per RRP-0012.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from rrxiv.server.deps import get_store
from rrxiv.server.errors import bad_request
from rrxiv.server.pagination import paginate
from rrxiv.server.papers.projection import to_list_item
from rrxiv.server.papers.scopes import filter_by_scope
from rrxiv.server.store import Store

router = APIRouter(prefix="/search", tags=["Search"])


@router.get("/papers")
def search_papers(
    request: Request,
    q: str = Query(..., min_length=1),
    limit: int = Query(default=20, ge=1, le=200),
    cursor: str | None = Query(default=None),
    scope: str | None = Query(default=None, description="Optional scope filter."),
    topic: str | None = Query(
        default=None, description="Restrict to papers whose topics[] contains this string."
    ),
    author: str | None = Query(
        default=None,
        description="Substring match against author name or ORCID.",
    ),
    status: str | None = Query(
        default=None,
        description="Filter to papers whose stats.status equals this value.",
    ),
    claims_min: int | None = Query(
        default=None,
        ge=0,
        description="Minimum number of claims.",
    ),
    submitted_from: str | None = Query(
        default=None,
        description="ISO date — only papers submitted on or after this date.",
    ),
    submitted_to: str | None = Query(
        default=None,
        description="ISO date — only papers submitted on or before this date.",
    ),
    sort: str | None = Query(
        default=None,
        description="One of: relevance (default) / newest / replicated / contested.",
    ),
) -> dict[str, Any]:
    if not q.strip():
        raise bad_request("query is empty")
    store: Store = get_store(request)
    needle = q.lower()

    pool: list[dict[str, Any]] = []
    for paper in store.list_papers():
        if not _paper_matches(paper, needle):
            continue
        pool.append(to_list_item(paper, store))

    if topic:
        pool = [item for item in pool if topic in (item.get("topics") or [])]
    if author:
        a_needle = author.lower()
        pool = [item for item in pool if _author_match(item, a_needle, author)]
    if status:
        pool = [
            item for item in pool if (item.get("stats") or {}).get("status") == status
        ]
    if claims_min is not None:
        pool = [
            item
            for item in pool
            if int((item.get("stats") or {}).get("claims") or 0) >= claims_min
        ]
    if submitted_from:
        pool = [
            item
            for item in pool
            if (item.get("submitted_at") or "") >= submitted_from
        ]
    if submitted_to:
        pool = [
            item
            for item in pool
            if (item.get("submitted_at") or "") <= submitted_to + "T23:59:59Z"
        ]
    if scope:
        pool = filter_by_scope(pool, scope)

    if sort == "newest":
        pool.sort(key=lambda x: x.get("submitted_at") or "", reverse=True)
    elif sort == "replicated":
        pool.sort(
            key=lambda x: int((x.get("stats") or {}).get("replicated") or 0),
            reverse=True,
        )
    elif sort == "contested":
        s = lambda x: int((x.get("stats") or {}).get("contested") or 0) + int(  # noqa: E731
            (x.get("stats") or {}).get("contradicted") or 0
        )
        pool.sort(key=s, reverse=True)
    # else: relevance/None — preserve corpus iteration order (a future
    # backend will compute a real score)

    page, next_cursor = paginate(
        pool,
        cursor=cursor,
        limit=limit,
        key=lambda p: (p.get("submitted_at") or "", p.get("id") or ""),
        order="desc",
    )
    return {"items": page, "next_cursor": next_cursor}


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

    page, next_cursor = paginate(
        matches,
        cursor=cursor,
        limit=limit,
        key=lambda c: (c.get("paper_id") or "", c.get("id") or ""),
        order="desc",
    )
    return {"items": page, "next_cursor": next_cursor}


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


def _author_match(item: dict[str, Any], needle_lower: str, needle_raw: str) -> bool:
    for author in item.get("authors") or []:
        name = (author.get("name") or "") if isinstance(author, dict) else str(author)
        orcid = (author.get("orcid") or "") if isinstance(author, dict) else ""
        if needle_lower in name.lower():
            return True
        if orcid and needle_raw in orcid:
            return True
    return False
