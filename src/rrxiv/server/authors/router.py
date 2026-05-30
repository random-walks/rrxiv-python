"""Authors router — GET /authors and /authors/{ident}, including agent handles.

Author identity in rrxiv is grounded in two namespaces:
- **Humans**: ORCID iDs (RRP-0006, RRP-0021)
- **Agents**: ``agent:*`` handles (RRP-0021, RRP-0026)

The ``/authors/{ident}`` endpoint dispatches on the ident shape:
- An ORCID iD → human profile (resolves via `author.orcid`).
- An ``agent:*`` handle → agent profile (resolves via `author.agent_handle`).
- Anything else → name fallback (legacy, still supported).

The response carries ``identity_type ∈ {"human", "agent", "name"}`` so
clients can render type-appropriate UI without re-sniffing the ident.

Sprint 26 / RRP-0028 additions:
- Agent-handle dispatch (was: ORCID + name only).
- ``identity_type`` field on every profile response.
- ``co_authors`` aggregation (distinct co-authors across the identity's papers).
- ``models`` aggregation (distinct models declared in this identity's
  provenance — surfaced on agent profiles for the model-history rail).
- New ``/authors/{ident}/papers`` and ``/authors/{ident}/claims``
  paginated sub-endpoints.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Query, Request

from rrxiv.server.claims.replication import apply_derived_status
from rrxiv.server.deps import get_store
from rrxiv.server.pagination import paginate
from rrxiv.server.papers.projection import to_list_item
from rrxiv.server.papers.slug import claim_owner_key
from rrxiv.server.store import Store

router = APIRouter(prefix="/authors", tags=["Authors"])

ORCID_PATTERN = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[0-9X]$")


def _is_orcid(value: str) -> bool:
    return bool(ORCID_PATTERN.match(value))


def _is_agent_handle(value: str) -> bool:
    """Agent handles use the ``agent:*`` namespace (or legacy ``@*``)."""
    return value.startswith("agent:") or value.startswith("@")


def _author_records(store: Store) -> dict[str, dict[str, Any]]:
    """Aggregate corpus authors keyed by ORCID, agent_handle, or name fallback.

    Key priority:
        1. ``agent:*`` handle (if the author has one)
        2. ORCID iD (if present and valid)
        3. ``name:<lowercase-name>`` fallback
    """
    by_key: dict[str, dict[str, Any]] = {}
    for paper in store.list_papers():
        for author in paper.get("authors") or []:
            if not isinstance(author, dict):
                continue
            name = author.get("name") or ""
            orcid = author.get("orcid") or ""
            handle = author.get("agent_handle") or ""
            is_agent = bool(author.get("is_agent")) or bool(handle)

            if handle and _is_agent_handle(handle):
                key = handle
                identity_type = "agent"
            elif orcid and _is_orcid(orcid):
                key = orcid
                identity_type = "human"
            else:
                key = f"name:{name.lower()}"
                identity_type = "name"

            entry = by_key.setdefault(
                key,
                {
                    "key": key,
                    "identity_type": identity_type,
                    "name": name,
                    "orcid": orcid if orcid and _is_orcid(orcid) else None,
                    "agent_handle": handle if handle and _is_agent_handle(handle) else None,
                    "is_agent": is_agent,
                    "paper_ids": [],
                    "co_authors": {},  # key -> {key, name, orcid, agent_handle, is_agent}
                    "models": {},  # release_pin or name -> ModelDescriptor
                },
            )
            entry["paper_ids"].append(paper["id"])
            if not entry["name"] and name:
                entry["name"] = name

            # Aggregate co-authors (everyone on this paper except self).
            for other in paper.get("authors") or []:
                if not isinstance(other, dict) or other is author:
                    continue
                other_name = other.get("name") or ""
                other_orcid = other.get("orcid") or ""
                other_handle = other.get("agent_handle") or ""
                if other_handle and _is_agent_handle(other_handle):
                    other_key = other_handle
                elif other_orcid and _is_orcid(other_orcid):
                    other_key = other_orcid
                else:
                    other_key = f"name:{other_name.lower()}"
                if other_key == key:
                    continue
                entry["co_authors"][other_key] = {
                    "key": other_key,
                    "name": other_name,
                    "orcid": other_orcid if other_orcid and _is_orcid(other_orcid) else None,
                    "agent_handle": (
                        other_handle
                        if other_handle and _is_agent_handle(other_handle)
                        else None
                    ),
                    "is_agent": bool(other.get("is_agent")) or bool(other_handle),
                }

            # Aggregate provenance.models[] entries for this identity.
            prov = author.get("provenance")
            if isinstance(prov, dict):
                models = prov.get("models")
                if isinstance(models, list):
                    for m in models:
                        if not isinstance(m, dict):
                            continue
                        mkey = (
                            str(m.get("release_pin") or "")
                            or str(m.get("name") or "").lower()
                        )
                        if not mkey:
                            continue
                        # Prefer the richer record (more fields wins).
                        existing = entry["models"].get(mkey)
                        if not existing or len(m) > len(existing):
                            entry["models"][mkey] = dict(m)
                elif prov.get("model_slug"):
                    # RRP-0025 legacy flat shape.
                    synth = {
                        "name": prov.get("model_slug"),
                        "release_pin": prov.get("model_slug"),
                        "family": prov.get("model_family"),
                    }
                    entry["models"][str(prov["model_slug"])] = synth
    return by_key


def _summarise_records(records: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Project the aggregation dict into a JSON-serialisable list."""
    out: list[dict[str, Any]] = []
    for r in records.values():
        out.append(
            {
                "key": r["key"],
                "identity_type": r["identity_type"],
                "name": r["name"],
                "orcid": r["orcid"],
                "agent_handle": r["agent_handle"],
                "is_agent": r["is_agent"],
                "paper_count": len(r["paper_ids"]),
            }
        )
    return out


@router.get("")
def list_authors(
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=200),
    identity_type: str | None = Query(
        default=None,
        description=(
            "Filter by identity type: 'human' (ORCID-keyed), 'agent' "
            "(handle-keyed), or 'name' (fallback). Omit for all."
        ),
    ),
) -> dict[str, Any]:
    store: Store = get_store(request)
    records = _author_records(store)
    items = _summarise_records(records)
    if identity_type:
        items = [i for i in items if i["identity_type"] == identity_type]
    items.sort(key=lambda a: (-a["paper_count"], (a["name"] or "").lower()))
    page, next_cursor = paginate(
        items,
        cursor=cursor,
        limit=limit,
        key=lambda a: (-a["paper_count"], (a["name"] or "").lower()),
        order="asc",
    )
    return {"items": page, "next_cursor": next_cursor}


def _resolve_record(
    store: Store, ident: str
) -> tuple[dict[str, Any] | None, str]:
    """Look up an author record by ident.

    Returns ``(record, identity_type)``. Identity type is one of
    ``"human" | "agent" | "name"``. If no record is found, returns
    ``(None, identity_type)`` where the type still reflects the ident
    shape — useful for empty-profile responses.
    """
    records = _author_records(store)
    if _is_agent_handle(ident):
        return records.get(ident), "agent"
    if _is_orcid(ident):
        return records.get(ident), "human"
    # Try the name-shaped key first (URL-decoded), then bare-name fallback.
    rec = records.get(ident) or records.get(f"name:{ident.lower()}")
    return rec, "name"


@router.get("/{ident}")
def get_author(ident: str, request: Request) -> dict[str, Any]:
    """Resolve an author profile by ORCID iD, agent handle, or name.

    The response carries ``identity_type`` so clients can branch:
    ``"human"`` → ORCID-keyed; ``"agent"`` → handle-keyed (with
    ``models`` aggregation); ``"name"`` → name fallback (legacy).
    """
    store: Store = get_store(request)
    record, identity_type = _resolve_record(store, ident)

    if record is None:
        return {
            "key": ident,
            "identity_type": identity_type,
            "name": ident,
            "orcid": ident if _is_orcid(ident) else None,
            "agent_handle": ident if _is_agent_handle(ident) else None,
            "is_agent": _is_agent_handle(ident),
            "paper_count": 0,
            "claim_count": 0,
            "papers": [],
            "claims": [],
            "co_authors": [],
            "models": [],
        }

    paper_ids = set(record["paper_ids"])
    authored_papers = [p for p in store.list_papers() if p["id"] in paper_ids]
    papers = [to_list_item(p, store) for p in authored_papers]
    papers.sort(key=lambda p: p.get("submitted_at") or "", reverse=True)

    # Claims are slug-keyed (claim.paper_id == id_slug per RRP-0013 /
    # RRP-0029), but record["paper_ids"] holds machine ``id``s (UUIDv7).
    # Map this identity's papers to their slug owner-keys and filter on
    # those.
    owner_keys = {claim_owner_key(p) for p in authored_papers}
    claims = [
        apply_derived_status(c, store)
        for c in store.list_claims()
        if c.get("paper_id") in owner_keys
    ]

    co_authors = sorted(
        record["co_authors"].values(),
        key=lambda c: (c.get("name") or "").lower(),
    )
    models = sorted(
        record["models"].values(),
        key=lambda m: (m.get("name") or "").lower(),
    )

    return {
        "key": record["key"],
        "identity_type": record["identity_type"],
        "name": record["name"],
        "orcid": record["orcid"],
        "agent_handle": record["agent_handle"],
        "is_agent": record["is_agent"],
        "paper_count": len(papers),
        "claim_count": len(claims),
        "papers": papers,
        "claims": claims,
        "co_authors": co_authors,
        "models": models,
    }


@router.get("/{ident}/papers")
def list_author_papers(
    ident: str,
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=200),
) -> dict[str, Any]:
    """Paginated paper list for this identity (RRP-0028)."""
    store: Store = get_store(request)
    record, _ = _resolve_record(store, ident)
    if record is None:
        return {"items": [], "next_cursor": None}

    paper_ids = set(record["paper_ids"])
    papers = [
        to_list_item(p, store)
        for p in store.list_papers()
        if p["id"] in paper_ids
    ]
    page, next_cursor = paginate(
        papers,
        cursor=cursor,
        limit=limit,
        key=lambda p: (p.get("submitted_at") or "", p.get("id") or ""),
        order="desc",
    )
    return {"items": page, "next_cursor": next_cursor}


@router.get("/{ident}/claims")
def list_author_claims(
    ident: str,
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=200),
) -> dict[str, Any]:
    """Paginated claims list for this identity (RRP-0028).

    Applies ``apply_derived_status`` so each claim's ``replication_status``
    reflects current annotation state (per RRP-0019/0020), matching the
    convention used by ``GET /papers/{id}/claims``.
    """
    store: Store = get_store(request)
    record, _ = _resolve_record(store, ident)
    if record is None:
        return {"items": [], "next_cursor": None}

    # Claims are slug-keyed (claim.paper_id == id_slug per RRP-0013 /
    # RRP-0029); record["paper_ids"] holds machine ``id``s (UUIDv7), so
    # map this identity's papers to their slug owner-keys first.
    paper_ids = set(record["paper_ids"])
    owner_keys = {
        claim_owner_key(p) for p in store.list_papers() if p["id"] in paper_ids
    }
    claims = [
        apply_derived_status(c, store)
        for c in store.list_claims()
        if c.get("paper_id") in owner_keys
    ]
    page, next_cursor = paginate(
        claims,
        cursor=cursor,
        limit=limit,
        key=lambda c: (c.get("paper_id") or "", c.get("id") or ""),
        order="desc",
    )
    return {"items": page, "next_cursor": next_cursor}
