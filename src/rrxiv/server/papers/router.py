"""Papers router — GET /papers, /papers/{id}, /papers/{id}/cir."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from rrxiv.server.deps import get_store
from rrxiv.server.errors import not_found
from rrxiv.server.store import Store

router = APIRouter(prefix="/papers", tags=["Papers"])


@router.get("")
def list_papers(request: Request) -> dict[str, Any]:
    store: Store = get_store(request)
    return {"items": store.list_papers(), "next_cursor": None}


@router.get("/{paper_id}")
def get_paper(paper_id: str, request: Request) -> dict[str, Any]:
    store: Store = get_store(request)
    paper = store.get_paper(paper_id)
    if paper is None:
        raise not_found(f"paper {paper_id} not found")
    return paper


@router.get("/{paper_id}/cir")
def get_cir(paper_id: str, request: Request) -> dict[str, Any]:
    store: Store = get_store(request)
    cir = store.get_cir(paper_id)
    if cir is None:
        # Fall back to the metadata-only view + empty annotations,
        # which the existing MockRrxivServer also does.
        paper = store.get_paper(paper_id)
        if paper is None:
            raise not_found(f"paper {paper_id} not found")
        cir = dict(paper)
        cir.setdefault("annotations", [])
    return cir
