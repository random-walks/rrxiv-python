"""Snapshots router — GET /snapshots/latest."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from rrxiv.server.deps import get_store
from rrxiv.server.errors import not_found
from rrxiv.server.store import Store

router = APIRouter(prefix="/snapshots", tags=["Snapshots"])


@router.get("/latest")
def latest_snapshot(request: Request) -> dict[str, Any]:
    store: Store = get_store(request)
    manifest = store.latest_snapshot()
    if manifest is None:
        raise not_found("no snapshot has been published")
    return manifest
