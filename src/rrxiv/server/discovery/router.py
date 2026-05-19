"""Discovery router — GET /scopes, GET /topics."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from rrxiv.server.deps import get_store
from rrxiv.server.papers.scopes import SCOPES
from rrxiv.server.store import Store

router = APIRouter(tags=["Discovery"])


@router.get("/scopes")
def list_scopes(request: Request) -> dict[str, Any]:
    """UI-discovery scopes published by this instance.

    Instance metadata, NOT protocol-binding. Other rrxiv instances may
    expose different scopes; clients read this endpoint rather than
    hardcoding a set.
    """
    return {"items": SCOPES, "next_cursor": None}


@router.get("/topics")
def list_topics(request: Request) -> dict[str, Any]:
    """Sorted unique list of topics present in the corpus.

    Derived from ``paper.topics[]`` across all papers. Useful for
    populating UI facets.
    """
    store: Store = get_store(request)
    topics: set[str] = set()
    for paper in store.list_papers():
        for topic in paper.get("topics") or []:
            if isinstance(topic, str) and topic:
                topics.add(topic)
    items = sorted(topics)
    return {"items": items, "next_cursor": None}
