"""Claims router — GET /claims, /claims/{id}, /claims/{id}/depends-on,
/claims/{id}/dependents."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from rrxiv.server.deps import get_store
from rrxiv.server.errors import not_found
from rrxiv.server.store import Store

router = APIRouter(prefix="/claims", tags=["Claims"])


@router.get("")
def list_claims(request: Request) -> dict[str, Any]:
    store: Store = get_store(request)
    return {"items": store.list_claims(), "next_cursor": None}


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
