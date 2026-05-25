"""Claims router — GET /claims, /claims/top, /claims/{id},
/claims/{id}/depends-on, /claims/{id}/dependents."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from rrxiv.server.claims.replication import apply_derived_status
from rrxiv.server.deps import get_store
from rrxiv.server.errors import not_found
from rrxiv.server.observability import tag
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
        [apply_derived_status(c, store) for c in store.list_claims()],
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
    """Top-N claims by graph influence.

    v0.1 ranking: ``dependents_count + replications + 0.5 * len(supports)``.

    - ``dependents_count`` is the number of other claims that declare
      ``depends_on: <this_claim_id>`` in the corpus. This is the
      structural answer to "which claims is everything else built on?"
      For Euclid, that surfaces I.4 (SAS), I.5, II.5, I.47, etc. — the
      foundational propositions other proofs lean on.
    - ``replications`` and ``supports`` are local-to-the-claim signals
      that tilt the ranking toward claims with attestation as well as
      structural use.

    The ``queries`` field on each item is also returned for backward
    compatibility with v0.1 web clients but is now the same as
    ``dependents_count`` — no longer a hash stub. When real
    request-telemetry lands (Plausible + per-claim event tracking,
    RRP-TBD) ``queries`` becomes a true count and ``dependents_count``
    stays a separate field.
    """
    store: Store = get_store(request)
    # Precompute incoming-edge counts in one pass.
    dependents: dict[str, int] = {}
    for claim in store.list_claims():
        for target in claim.get("depends_on") or []:
            tid = str(target)
            dependents[tid] = dependents.get(tid, 0) + 1

    ranked: list[tuple[float, int, dict[str, Any]]] = []
    for claim in store.list_claims():
        cid = str(claim.get("id"))
        replications = len(claim.get("replications") or []) if isinstance(
            claim.get("replications"), list
        ) else int(claim.get("replications") or 0)
        supports = len(claim.get("supports") or [])
        d = dependents.get(cid, 0)
        score = float(d) + float(replications) + 0.5 * float(supports)
        ranked.append((score, d, claim))
    ranked.sort(key=lambda triple: triple[0], reverse=True)

    items: list[dict[str, Any]] = []
    for _score, dep_count, claim in ranked[:limit]:
        cid = str(claim.get("id"))
        items.append(
            {
                "id": cid,
                "statement": claim.get("statement", ""),
                "dependents_count": dep_count,
                # Backward-compat alias for v0.1 web clients that still
                # read ``queries``. Drops once everyone reads
                # ``dependents_count`` directly.
                "queries": dep_count,
            }
        )
    return {"items": items, "next_cursor": None}


@router.get("/{claim_id}")
def get_claim(claim_id: str, request: Request) -> dict[str, Any]:
    tag("claim_id", claim_id)
    store: Store = get_store(request)
    c = store.get_claim(claim_id)
    if c is None:
        raise not_found(f"claim {claim_id} not found")
    # Sprint 22: bump the per-claim view counter and stamp the new
    # total onto the response. Wrapped in try/except because telemetry
    # MUST never break a read path — if the store hiccups we still
    # serve the claim. The leaderboard in /stats/pulse reads from the
    # same counter so the dashboard sees real engagement.
    try:
        views = store.bump_claim_view(claim_id)
    except Exception:
        views = 0
    payload = apply_derived_status(c, store)
    if isinstance(payload, dict):
        payload["views_count"] = views
    return payload


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
