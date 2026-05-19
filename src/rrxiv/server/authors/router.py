"""Authors router — GET /authors/{ident} and /authors.

Author identity in rrxiv is grounded in ORCID. For papers that carry
an ``orcid`` on their ``authors[]`` entries we can resolve those to
profile pages; for legacy or anonymous-paper authors we resolve by
name match instead.

v0.1 behaviour:
- ``GET /authors``: list authors derived from the corpus (sorted by
  paper count desc).
- ``GET /authors/{orcid}``: papers + claims authored by this ORCID.
- ``GET /authors/by-name/{name}``: fallback name-based lookup.

No identity-store yet — these are pure derived views over the
existing papers/claims data.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Query, Request

from rrxiv.server.deps import get_store
from rrxiv.server.pagination import paginate
from rrxiv.server.papers.projection import to_list_item
from rrxiv.server.store import Store

router = APIRouter(prefix="/authors", tags=["Authors"])

ORCID_PATTERN = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[0-9X]$")


def _is_orcid(value: str) -> bool:
    return bool(ORCID_PATTERN.match(value))


def _author_records(store: Store) -> dict[str, dict[str, Any]]:
    """Aggregate corpus authors keyed by ORCID (or name fallback).

    The key is the ORCID when available; otherwise a synthetic
    ``name:<lowercase-name>`` key so name-only authors stay groupable.
    """
    by_key: dict[str, dict[str, Any]] = {}
    for paper in store.list_papers():
        for author in paper.get("authors") or []:
            if not isinstance(author, dict):
                continue
            name = author.get("name") or ""
            orcid = author.get("orcid") or ""
            is_agent = bool(author.get("is_agent"))
            key = orcid if orcid and _is_orcid(orcid) else f"name:{name.lower()}"
            entry = by_key.setdefault(
                key,
                {
                    "key": key,
                    "name": name,
                    "orcid": orcid if orcid and _is_orcid(orcid) else None,
                    "is_agent": is_agent,
                    "paper_ids": [],
                },
            )
            entry["paper_ids"].append(paper["id"])
            # Prefer non-empty name from later papers if earlier was blank.
            if not entry["name"] and name:
                entry["name"] = name
    return by_key


@router.get("")
def list_authors(
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=200),
) -> dict[str, Any]:
    store: Store = get_store(request)
    records = _author_records(store)
    items = [
        {
            "key": r["key"],
            "name": r["name"],
            "orcid": r["orcid"],
            "is_agent": r["is_agent"],
            "paper_count": len(r["paper_ids"]),
        }
        for r in records.values()
    ]
    # Sort by paper count desc, then name asc.
    items.sort(key=lambda a: (-a["paper_count"], a["name"].lower()))
    page, next_cursor = paginate(
        items,
        cursor=cursor,
        limit=limit,
        key=lambda a: (-a["paper_count"], a["name"].lower()),
        order="asc",
    )
    return {"items": page, "next_cursor": next_cursor}


@router.get("/{ident}")
def get_author(ident: str, request: Request) -> dict[str, Any]:
    """Resolve an author profile by ORCID."""
    store: Store = get_store(request)
    records = _author_records(store)
    record = records.get(ident)
    if record is None:
        # Could still be a name lookup — try that fallback.
        record = records.get(f"name:{ident.lower()}")
    if record is None:
        return {
            "key": ident,
            "name": ident,
            "orcid": ident if _is_orcid(ident) else None,
            "is_agent": False,
            "papers": [],
            "claims": [],
        }
    papers = [
        to_list_item(p, store)
        for p in store.list_papers()
        if p["id"] in record["paper_ids"]
    ]
    paper_ids = set(record["paper_ids"])
    claims = [
        c for c in store.list_claims() if c.get("paper_id") in paper_ids
    ]
    return {
        "key": record["key"],
        "name": record["name"],
        "orcid": record["orcid"],
        "is_agent": record["is_agent"],
        "paper_count": len(papers),
        "claim_count": len(claims),
        "papers": papers,
        "claims": claims,
    }
