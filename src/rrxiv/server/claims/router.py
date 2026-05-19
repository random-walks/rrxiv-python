"""Claims router — GET /claims, /claims/top, /claims/{id},
/claims/{id}/depends-on, /claims/{id}/dependents."""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, Query, Request

from rrxiv.server.deps import get_store
from rrxiv.server.errors import not_found
from rrxiv.server.pagination import paginate
from rrxiv.server.store import Store

router = APIRouter(prefix="/claims", tags=["Claims"])


@router.get("")
def list_claims(
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=200),
) -> dict[str, Any]:
    store: Store = get_store(request)
    page, next_cursor = paginate(
        store.list_claims(),
        cursor=cursor,
        limit=limit,
        key=lambda c: (c.get("paper_id") or "", c.get("id") or ""),
        order="desc",
    )
    return {"items": page, "next_cursor": next_cursor}


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


def _outgoing(store: Store, claim_id: str, field: str, kind: str) -> list[dict[str, str]]:
    origin = store.get_claim(claim_id)
    if origin is None:
        return []
    return [
        {"source": claim_id, "target": str(t), "kind": kind}
        for t in (origin.get(field) or [])
    ]


def _incoming(store: Store, claim_id: str, field: str, kind: str) -> list[dict[str, str]]:
    edges: list[dict[str, str]] = []
    for c in store.list_claims():
        if claim_id in (c.get(field) or []):
            edges.append(
                {"source": str(c.get("id") or ""), "target": claim_id, "kind": kind}
            )
    return edges


@router.get("/{claim_id}/depends-on")
def claim_depends_on(claim_id: str, request: Request) -> dict[str, Any]:
    store: Store = get_store(request)
    return {
        "origin": claim_id,
        "edges": _outgoing(store, claim_id, "depends_on", "depends_on"),
    }


@router.get("/{claim_id}/dependents")
def claim_dependents(claim_id: str, request: Request) -> dict[str, Any]:
    store: Store = get_store(request)
    return {
        "origin": claim_id,
        "edges": _incoming(store, claim_id, "depends_on", "depends_on"),
    }


@router.get("/{claim_id}/supports")
def claim_supports(claim_id: str, request: Request) -> dict[str, Any]:
    """Outgoing 'supports' edges from this claim."""
    store: Store = get_store(request)
    return {
        "origin": claim_id,
        "edges": _outgoing(store, claim_id, "supports", "supports"),
    }


@router.get("/{claim_id}/supported-by")
def claim_supported_by(claim_id: str, request: Request) -> dict[str, Any]:
    """Inverse — claims declaring 'supports' that include this claim."""
    store: Store = get_store(request)
    return {
        "origin": claim_id,
        "edges": _incoming(store, claim_id, "supports", "supports"),
    }


@router.get("/{claim_id}/contradicts")
def claim_contradicts(claim_id: str, request: Request) -> dict[str, Any]:
    """Outgoing 'contradicts' edges from this claim."""
    store: Store = get_store(request)
    return {
        "origin": claim_id,
        "edges": _outgoing(store, claim_id, "contradicts", "contradicts"),
    }


@router.get("/{claim_id}/contradicted-by")
def claim_contradicted_by(claim_id: str, request: Request) -> dict[str, Any]:
    """Inverse — claims declaring 'contradicts' that include this claim."""
    store: Store = get_store(request)
    return {
        "origin": claim_id,
        "edges": _incoming(store, claim_id, "contradicts", "contradicts"),
    }


@router.get("/{claim_id}/extends")
def claim_extends(claim_id: str, request: Request) -> dict[str, Any]:
    """Outgoing 'extends' edges from this claim."""
    store: Store = get_store(request)
    return {
        "origin": claim_id,
        "edges": _outgoing(store, claim_id, "extends", "extends"),
    }


@router.get("/{claim_id}/extended-by")
def claim_extended_by(claim_id: str, request: Request) -> dict[str, Any]:
    """Inverse — claims declaring 'extends' that include this claim."""
    store: Store = get_store(request)
    return {
        "origin": claim_id,
        "edges": _incoming(store, claim_id, "extends", "extends"),
    }


@router.get("/{claim_id}/neighborhood")
def claim_neighborhood(claim_id: str, request: Request) -> dict[str, Any]:
    """All claim-to-claim edges touching this claim, in one round-trip.

    Useful for the claim detail view where the UI wants depends_on /
    dependents / supports / supported-by / contradicts / contradicted-by /
    extends / extended-by all at once.
    """
    store: Store = get_store(request)
    origin = store.get_claim(claim_id)
    if origin is None:
        return {
            "origin": claim_id,
            "depends_on": [],
            "dependents": [],
            "supports": [],
            "supported_by": [],
            "contradicts": [],
            "contradicted_by": [],
            "extends": [],
            "extended_by": [],
        }
    return {
        "origin": claim_id,
        "depends_on": _outgoing(store, claim_id, "depends_on", "depends_on"),
        "dependents": _incoming(store, claim_id, "depends_on", "depends_on"),
        "supports": _outgoing(store, claim_id, "supports", "supports"),
        "supported_by": _incoming(store, claim_id, "supports", "supports"),
        "contradicts": _outgoing(store, claim_id, "contradicts", "contradicts"),
        "contradicted_by": _incoming(store, claim_id, "contradicts", "contradicts"),
        "extends": _outgoing(store, claim_id, "extends", "extends"),
        "extended_by": _incoming(store, claim_id, "extends", "extends"),
    }
