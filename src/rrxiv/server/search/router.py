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
from rrxiv.server.pagination import paginate
from rrxiv.server.papers.projection import to_list_item
from rrxiv.server.papers.scopes import filter_by_scope
from rrxiv.server.store import Store

router = APIRouter(prefix="/search", tags=["Search"])


@router.get("/papers")
def search_papers(
    request: Request,
    q: str = Query(
        default="",
        max_length=200,
        description=(
            "Free-text needle. Matched case-insensitively against title, "
            "abstract, author names, and topics. Empty string (the default) "
            "means match-all — combine with the targeted filters below to "
            "browse the corpus without a textual query (RRP-0028)."
        ),
    ),
    limit: int = Query(default=20, ge=1, le=200),
    cursor: str | None = Query(default=None),
    scope: str | None = Query(default=None, description="Optional scope filter."),
    topic: str | None = Query(
        default=None, description="Restrict to papers whose topics[] contains this string."
    ),
    author: str | None = Query(
        default=None,
        description=(
            "Substring match against author name or ORCID (legacy; prefer the "
            "targeted filters). RRP-0028: comma-separated values mean OR — "
            "`author=Blaise,Claude` returns the union, not the intersection."
        ),
    ),
    orcid: str | None = Query(
        default=None,
        description=(
            "RRP-0026: exact match against author.orcid. "
            "RRP-0028: comma-separated values OR-combine."
        ),
    ),
    agent_handle: str | None = Query(
        default=None,
        description=(
            "RRP-0026: exact match against author.agent_handle. "
            "RRP-0028: comma-separated values OR-combine."
        ),
    ),
    model_family: str | None = Query(
        default=None,
        description=(
            "RRP-0026: exact match against any provenance.models[].family "
            "(lowercase). RRP-0028: comma-separated values OR-combine."
        ),
    ),
    model_name: str | None = Query(
        default=None,
        description=(
            "RRP-0026: case-insensitive substring match against any "
            "provenance.models[].name. RRP-0028: comma-separated values "
            "OR-combine."
        ),
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
    store: Store = get_store(request)
    needle = q.strip().lower()

    # RRP-0028 default-match: empty q returns the full corpus filtered
    # by the other params. The textual needle is only applied when
    # present, so `?author=Claude+Opus+4.7` alone still works.
    pool: list[dict[str, Any]] = []
    for paper in store.list_papers():
        if needle and not _paper_matches(paper, needle):
            continue
        pool.append(to_list_item(paper, store))

    if topic:
        pool = [item for item in pool if topic in (item.get("topics") or [])]
    # RRP-0028: comma-separated values on the author-shaped filters mean
    # OR (union). Single values are the n=1 case.
    if author:
        needles = _csv_or_split(author)
        if needles:
            pool = [
                item
                for item in pool
                if any(_author_match(item, n.lower(), n) for n in needles)
            ]
    if orcid:
        ids = _csv_or_split(orcid)
        if ids:
            pool = [item for item in pool if any(_has_orcid(item, i) for i in ids)]
    if agent_handle:
        handles = _csv_or_split(agent_handle)
        if handles:
            pool = [
                item
                for item in pool
                if any(_has_agent_handle(item, h) for h in handles)
            ]
    if model_family:
        families = [f.lower() for f in _csv_or_split(model_family)]
        if families:
            pool = [
                item
                for item in pool
                if any(_has_model_family(item, f) for f in families)
            ]
    if model_name:
        needles_mn = [n.lower() for n in _csv_or_split(model_name)]
        if needles_mn:
            pool = [
                item
                for item in pool
                if any(_has_model_name_substring(item, n) for n in needles_mn)
            ]
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
    q: str = Query(
        default="",
        max_length=200,
        description="Free-text needle. Empty string means match-all (RRP-0028).",
    ),
    limit: int = Query(default=20, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> dict[str, Any]:
    store: Store = get_store(request)
    needle = q.strip().lower()
    matches: list[dict[str, Any]] = []
    for claim in store.list_claims():
        if not needle:
            matches.append(claim)
            continue
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


def _csv_or_split(value: str) -> list[str]:
    """Split a comma-separated query-param value into OR needles.

    RRP-0028: ``?author=A,B`` means "papers with author A or author B",
    not "papers with both". Trims whitespace; empties dropped.
    """
    return [v.strip() for v in value.split(",") if v.strip()]


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


# RRP-0026: targeted filter helpers — exact-match for ORCID + agent_handle
# + model_family; substring for model_name.


def _has_orcid(item: dict[str, Any], orcid_id: str) -> bool:
    for author in item.get("authors") or []:
        if isinstance(author, dict) and (author.get("orcid") == orcid_id):
            return True
    return False


def _has_agent_handle(item: dict[str, Any], handle: str) -> bool:
    for author in item.get("authors") or []:
        if isinstance(author, dict) and (author.get("agent_handle") == handle):
            return True
    return False


def _iter_provenance_models(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten every ModelDescriptor across every agent author."""
    out: list[dict[str, Any]] = []
    for author in item.get("authors") or []:
        if not isinstance(author, dict):
            continue
        prov = author.get("provenance")
        if not isinstance(prov, dict):
            continue
        models = prov.get("models")
        if isinstance(models, list):
            for m in models:
                if isinstance(m, dict):
                    out.append(m)
        # RRP-0025 back-compat: synthesise a single-model descriptor
        # from flat fields if `models` is absent.
        elif "model_slug" in prov or "model_family" in prov:
            synth: dict[str, Any] = {}
            if prov.get("model_slug"):
                synth["name"] = prov["model_slug"]
                synth["release_pin"] = prov["model_slug"]
            if prov.get("model_family"):
                synth["family"] = prov["model_family"]
            if synth:
                out.append(synth)
    return out


def _has_model_family(item: dict[str, Any], family_lower: str) -> bool:
    for m in _iter_provenance_models(item):
        if (m.get("family") or "").lower() == family_lower:
            return True
    return False


def _has_model_name_substring(item: dict[str, Any], needle_lower: str) -> bool:
    for m in _iter_provenance_models(item):
        if needle_lower in (m.get("name") or "").lower():
            return True
    return False
