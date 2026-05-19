"""Claims router — GET /claims, /claims/top, /claims/{id},
/claims/{id}/depends-on, /claims/{id}/dependents."""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, Query, Request

from rrxiv.server.deps import get_store
from rrxiv.server.errors import not_found
from rrxiv.server.store import Store

router = APIRouter(prefix="/claims", tags=["Claims"])


@router.get("")
def list_claims(request: Request) -> dict[str, Any]:
    store: Store = get_store(request)
    return {"items": store.list_claims(), "next_cursor": None}


@router.get("/top")
def list_top_claims(
    request: Request,
    limit: int = Query(5, ge=1, le=50),
) -> dict[str, Any]:
    """Top-N claims by replication interest.

    v0.1 ranking: ``replications + 0.5 * len(supports)``. The ``queries``
    field on each item is a deterministic stub (hash-of-id mod 4000) until
    real query telemetry lands. The shape matches what the web client's
    home page consumes.
    """
    store: Store = get_store(request)
    ranked: list[tuple[float, dict[str, Any]]] = []
    for claim in store.list_claims():
        replications = len(claim.get("replications") or []) if isinstance(
            claim.get("replications"), list
        ) else int(claim.get("replications") or 0)
        supports = len(claim.get("supports") or [])
        score = float(replications) + 0.5 * float(supports)
        ranked.append((score, claim))
    ranked.sort(key=lambda pair: pair[0], reverse=True)

    items: list[dict[str, Any]] = []
    for _, claim in ranked[:limit]:
        cid = str(claim.get("id"))
        # Deterministic stub: hash-of-id mod 4000 gives a stable
        # "queries" number across requests. Real telemetry replaces this.
        digest = hashlib.sha256(cid.encode("utf-8")).digest()
        queries = (int.from_bytes(digest[:4], "big") % 4000) + 50
        items.append(
            {
                "id": cid,
                "statement": claim.get("statement", ""),
                "queries": queries,
            }
        )
    return {"items": items, "next_cursor": None}


@router.get("/{claim_id}")
def get_claim(claim_id: str, request: Request) -> dict[str, Any]:
    store: Store = get_store(request)
    c = store.get_claim(claim_id)
    if c is None:
        raise not_found(f"claim {claim_id} not found")
    return c


@router.get("/{claim_id}/depends-on")
def claim_depends_on(claim_id: str, request: Request) -> dict[str, Any]:
    store: Store = get_store(request)
    origin = store.get_claim(claim_id)
    edges: list[dict[str, str]] = []
    if origin is not None:
        for target in origin.get("depends_on") or []:
            edges.append(
                {"source": claim_id, "target": str(target), "kind": "depends_on"}
            )
    return {"origin": claim_id, "edges": edges}


@router.get("/{claim_id}/dependents")
def claim_dependents(claim_id: str, request: Request) -> dict[str, Any]:
    store: Store = get_store(request)
    edges: list[dict[str, str]] = []
    for cid, claim in {c["id"]: c for c in store.list_claims()}.items():
        if claim_id in (claim.get("depends_on") or []):
            edges.append(
                {"source": cid, "target": claim_id, "kind": "depends_on"}
            )
    return {"origin": claim_id, "edges": edges}
